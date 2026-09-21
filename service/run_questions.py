#!/usr/bin/env python3
"""Run questions; record operational failures separately, never as corpus refusals."""

import argparse
import errno
import json
import os
import tempfile
from pathlib import Path
import time
import urllib.error
import urllib.request

from config import MODEL, PORT, QUERY_TIMEOUT


COUNTERS = ("model_calls", "api_attempts", "input_tokens", "output_tokens")


def read_jsonl(path):
    if not path.exists():
        return []
    return [json.loads(line) for line in path.read_text().splitlines() if line.strip()]


def by_id(records, label):
    result = {}
    for record in records:
        key = record["id"]
        if key in result:
            raise ValueError(f"Duplicate question ID in {label}: {key}")
        result[key] = record
    return result


def atomic_write(path, text):
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = None
    try:
        with tempfile.NamedTemporaryFile(mode="w", dir=path.parent, delete=False) as f:
            temporary = Path(f.name)
            f.write(text)
            f.flush()
            os.fsync(f.fileno())
        temporary.replace(path)
    finally:
        if temporary is not None:
            temporary.unlink(missing_ok=True)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--questions", required=True)
    parser.add_argument("--out", required=True)
    parser.add_argument("--url", default=f"http://127.0.0.1:{PORT}",
                        help="Service base URL (defaults to PORT from .env)")
    parser.add_argument("--limit", type=int, default=0, help="Maximum pending questions to run")
    parser.add_argument("--resume", action="store_true",
                        help="Keep saved answers and rerun only failed or missing questions")
    parser.add_argument(
        "--invalid-response-retries", type=int, choices=(0, 1, 2), default=2,
        help="Fresh query retries after invalid_model_response (default: 2)",
    )
    args = parser.parse_args()
    if args.limit < 0:
        parser.error("--limit cannot be negative")
    questions = read_jsonl(Path(args.questions))
    if not questions:
        parser.error("The questions file is missing or empty")
    question_map = by_id(questions, "questions")
    out = Path(args.out)
    errors_path = out.with_name(out.stem + "_errors.jsonl")
    manifest_path = out.with_name(out.stem + "_manifest.json")
    saved = by_id(read_jsonl(out), "answers") if args.resume else {}
    old_manifest = json.loads(manifest_path.read_text()) if args.resume and manifest_path.exists() else {}
    usages = by_id(old_manifest.get("per_question", []), "manifest")
    failures = by_id(old_manifest.get("errors", []), "manifest errors")
    # Older runners wrote errors incrementally but only wrote the manifest at the end.
    if args.resume and not old_manifest:
        failures = by_id(read_jsonl(errors_path), "errors")
    known_ids = set(saved) | set(usages) | set(failures)
    if known_ids - question_map.keys():
        parser.error("Saved results contain IDs outside this questions file; use a different --out")
    if any(answer.get("status") not in ("answered", "refused") for answer in saved.values()):
        parser.error("Saved answers contain invalid statuses; fix the file before resuming")
    requests = {
        key: {field: q[field] for field in ("id", "question", "role", "as_of")}
        for key, q in question_map.items()
    }
    for key, previous in old_manifest.get("requests", {}).items():
        if requests.get(key) != previous:
            parser.error(f"Question {key} changed; use a different --out for the new run")
    for key in saved:
        failures.pop(key, None)
        if key not in usages:
            # Existing answers remain useful, but their missing usage cannot be reconstructed.
            usages[key] = {"id": key, "model": old_manifest.get("model", MODEL),
                           **dict.fromkeys(COUNTERS), "latency_s": None,
                           "query_attempts": None, "retry_errors": [], "usage_complete": False}

    def checkpoint():
        pending_ids = [key for key in question_map if key not in saved]
        usage_rows = [usages[key] for key in question_map if key in usages]
        error_rows = [failures[key] for key in question_map if key in failures]
        manifest = {
            "model": MODEL, "per_question": usage_rows, "errors": error_rows,
            "pending_ids": pending_ids, "complete": not pending_ids,
            "usage_complete": all(row.get("usage_complete", True) for row in usage_rows)
                and all(row.get("usage", {}).get("usage_complete", False) for row in error_rows),
            "requests": requests,
        }
        # Write usage first: if interrupted between files, a missing answer is retried with
        # its already-recorded cost retained, rather than losing the previous model usage.
        atomic_write(manifest_path, json.dumps(manifest, indent=2))
        atomic_write(out, "".join(json.dumps(saved[key], ensure_ascii=False) + "\n"
                                   for key in question_map if key in saved))
        atomic_write(errors_path, "".join(json.dumps(row) + "\n" for row in error_rows))

    pending = [q for q in questions if q["id"] not in saved]
    if args.limit:
        pending = pending[:args.limit]
    print(f"Saved: {len(saved)}; running: {len(pending)}; total: {len(questions)}", flush=True)
    checkpoint()
    for i, question in enumerate(pending):
        request = urllib.request.Request(
            args.url.rstrip("/") + "/ask?trace=true",
            data=json.dumps(
                {k: question[k] for k in ("id", "question", "role", "as_of")}
            ).encode(),
            headers={"Content-Type": "application/json"},
        )
        start = time.monotonic()
        previous_error = failures.get(question["id"])
        previous_usage = usages.pop(question["id"], None)
        if previous_error:
            previous_usage = previous_error.get("usage", {})
        totals = {key: (previous_usage.get(key) if previous_usage is not None else 0)
                  for key in COUNTERS}
        previous_latency = (previous_usage or {}).get("latency_s", 0)
        if previous_error:
            previous_latency = previous_error.get("latency_s", previous_latency)
        previous_attempts = (previous_usage or {}).get("query_attempts", 0)
        retry_errors = list((previous_usage or {}).get("retry_errors", []))
        if previous_error:
            previous_attempts = previous_error.get("query_attempts", 1)
            retry_errors = list(previous_error.get("retry_errors", []))
            retry_errors.append({
                **{key: value for key, value in previous_error.items()
                   if key not in ("id", "retry_errors", "usage")},
                "usage": {key: value for key, value in (previous_usage or {}).items()
                          if key != "retry_errors"},
            })

        def total_usage(attempt):
            return {
                "model": (previous_usage or {}).get("model", MODEL), **totals,
                "latency_s": (round(previous_latency + time.monotonic() - start, 3)
                              if previous_latency is not None else None),
                "query_attempts": (previous_attempts + attempt + 1
                                   if previous_attempts is not None else None),
                "retry_errors": list(retry_errors),
                "usage_complete": all(value is not None for value in totals.values())
                                  and previous_latency is not None,
            }
        for attempt in range(args.invalid_response_retries + 1):
            # If stopped while the request is running, the server may still spend tokens.
            # Keep the question pending and explicitly mark that attempt's usage unknown.
            interrupted_usage = total_usage(attempt)
            interrupted_usage.update(dict.fromkeys(COUNTERS))
            interrupted_usage.update(latency_s=None, usage_complete=False)
            failures[question["id"]] = {
                "id": question["id"], "error": "request_incomplete",
                "message": "No completed response saved; this attempt's usage is unknown.",
                "usage": interrupted_usage, "latency_s": None,
                "query_attempts": interrupted_usage["query_attempts"],
                "retry_errors": list(retry_errors),
            }
            checkpoint()
            try:
                with urllib.request.urlopen(
                    request, timeout=QUERY_TIMEOUT + 15
                ) as response:
                    payload = json.loads(response.read())
                answer = payload["response"]
                if answer.get("id") != question["id"] or answer.get("status") not in ("answered", "refused"):
                    raise ValueError("Invalid service response")
                attempt_usage = payload["usage"]
                for key in totals:
                    value = attempt_usage.get(key)
                    totals[key] = (totals[key] + value
                                   if totals[key] is not None and value is not None else None)
            except (
                urllib.error.URLError,
                TimeoutError,
                OSError,
                ValueError,
                KeyError,
                TypeError,
                AttributeError,
            ) as exc:
                error = {
                    "id": question["id"],
                    "error": "request_failed",
                    "latency_s": round(time.monotonic() - start, 3),
                }
                if isinstance(exc, urllib.error.HTTPError):
                    error["http_status"] = exc.code
                    try:
                        body = json.loads(exc.read())
                        error["error"] = body.get("error", "request_failed")
                        if isinstance(body.get("usage"), dict):
                            error["usage"] = body["usage"]
                        if isinstance(body.get("diagnostics"), dict):
                            error["diagnostics"] = body["diagnostics"]
                    except (ValueError, AttributeError):
                        pass
                    finally:
                        exc.close()
                elif isinstance(exc, (urllib.error.URLError, OSError)):
                    reason = exc.reason if isinstance(exc, urllib.error.URLError) else exc
                    if isinstance(reason, ConnectionRefusedError) or getattr(reason, "errno", None) == errno.ECONNREFUSED:
                        error["error"] = "connection_refused"
                        error["message"] = "Cannot connect to the service. Check that it is running and --url matches its port."
                    elif isinstance(reason, TimeoutError):
                        error["error"] = "request_timeout"
                        error["message"] = "The service did not respond before the client timeout."
                    else:
                        error["error"] = "connection_failed"
                        error["message"] = "Could not reach the service. Check its address and network connection."
                attempt_usage = error.get("usage", {})
                for key in totals:
                    value = attempt_usage.get(key)
                    totals[key] = (totals[key] + value
                                   if totals[key] is not None and value is not None else None)
                error["usage"] = total_usage(attempt)
                error["latency_s"] = error["usage"]["latency_s"]
                error["query_attempts"] = error["usage"]["query_attempts"]
                error["retry_errors"] = list(retry_errors)
                failures[question["id"]] = error
                checkpoint()
                if (
                    error.get("http_status") == 502
                    and error["error"] == "invalid_model_response"
                    and attempt < args.invalid_response_retries
                ):
                    retry_errors.append({
                        "attempt": attempt + 1,
                        "error": error["error"],
                        "http_status": error["http_status"],
                        "usage": attempt_usage,
                        **({"diagnostics": error["diagnostics"]} if "diagnostics" in error else {}),
                    })
                    print(
                        f"[{i+1}/{len(pending)}] {question['id']}: invalid_model_response; "
                        f"restarting query (retry {attempt+1}/{args.invalid_response_retries})",
                        flush=True,
                    )
                    continue
                print(
                    f"[{i+1}/{len(pending)}] {question['id']}: ERROR {error['error']}"
                    + (f" — {error['message']}" if error.get("message") else ""),
                    flush=True,
                )
                break
            else:
                usage = total_usage(attempt)
                usage["model"] = attempt_usage.get("model", MODEL)
                saved[question["id"]] = answer
                failures.pop(question["id"], None)
                usages[question["id"]] = {"id": question["id"], **usage}
                checkpoint()
                print(
                    f"[{i+1}/{len(pending)}] {question['id']}: {answer['status']}",
                    flush=True,
                )
                break
    checkpoint()
    print(f"Completed: {len(saved)}/{len(questions)} responses; {len(failures)} errors")
    if any(not row.get("usage_complete", True) for row in usages.values()):
        print("Some saved usage is unavailable; the manifest marks it unknown rather than estimating it.")
    raise SystemExit(1 if len(saved) != len(questions) else 0)


if __name__ == "__main__":
    main()
