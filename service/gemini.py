"""Shared Gemini transport with bounded retries, tool calls, and usage accounting."""

from contextlib import contextmanager
import json
import logging
import re
import threading
import time
import urllib.error
import urllib.request

from config import BASE_URL, MODEL, GEMINI_API_KEY, GEMINI_CALL_DELAY_SECONDS

log = logging.getLogger(__name__)
_call_lock = threading.Lock()
_next_call_at = 0.0


class ServiceError(Exception):
    def __init__(self, code, message, status=503):
        super().__init__(message)
        self.code, self.message, self.status = code, message, status

    def payload(self):
        result = {
            "error": self.code,
            "message": self.message,
            "retryable": self.status in (502, 503),
        }
        if hasattr(self, "usage"):
            result["usage"] = self.usage
        if hasattr(self, "diagnostics"):
            result["diagnostics"] = self.diagnostics
        return result


@contextmanager
def paced_call(deadline):
    """Keep the configured gap after each HTTP attempt, shared across requests."""
    global _next_call_at
    timeout = max(0, deadline - time.monotonic()) if deadline else -1
    if not _call_lock.acquire(timeout=timeout):
        raise ServiceError("request_timeout", "The request took too long. Please retry.")
    try:
        delay = max(0, _next_call_at - time.monotonic())
        if deadline and time.monotonic() + delay >= deadline:
            raise ServiceError("request_timeout", "The request took too long. Please retry.")
        if delay:
            time.sleep(delay)
        if deadline and time.monotonic() >= deadline:
            raise ServiceError("request_timeout", "The request took too long. Please retry.")
        try:
            yield
        finally:
            _next_call_at = time.monotonic() + GEMINI_CALL_DELAY_SECONDS
    finally:
        _call_lock.release()


def decode_json(message):
    try:
        content = (message.get("content") or "").strip()
        fenced = re.fullmatch(r"```(?:json)?\s*\n(.*?)\n```", content, re.S)
        value = json.loads(fenced[1] if fenced else content)
        if not isinstance(value, dict):
            raise ValueError("Expected an object")
        return value
    except (ValueError, TypeError, AttributeError) as exc:
        raise ServiceError(
            "invalid_model_response",
            "The model returned an invalid response. Please retry.",
            502,
        ) from exc


class GeminiClient:
    def __init__(self, api_key=None, model=MODEL, base_url=BASE_URL, deadline=None):
        self.api_key = GEMINI_API_KEY if api_key is None else api_key
        if not self.api_key:
            raise ServiceError(
                "configuration_error", "The model API key is not configured.", 500
            )
        self.model, self.base_url, self.deadline = model, base_url, deadline
        self.started = time.monotonic()
        self.calls = self.attempts = self.input_tokens = self.output_tokens = 0

    def usage(self):
        return {
            "model": self.model,
            "model_calls": self.calls,
            "api_attempts": self.attempts,
            "input_tokens": self.input_tokens,
            "output_tokens": self.output_tokens,
            "latency_s": round(time.monotonic() - self.started, 3),
        }

    def complete(
        self,
        messages,
        *,
        tools=None,
        tool_choice="auto",
        json_output=False,
        schema=None,
        timeout=60,
    ):
        body = {"model": self.model, "messages": messages}
        if tools:
            body.update(tools=tools, tool_choice=tool_choice)
        if schema:
            body["response_format"] = {
                "type": "json_schema",
                "json_schema": {"name": "response", "strict": True, "schema": schema},
            }
        elif json_output:
            body["response_format"] = {"type": "json_object"}
        encoded = json.dumps(body).encode()
        if len(encoded) > 700_000:
            raise ServiceError(
                "context_limit",
                "The evidence exceeds this service's context budget. Please narrow the question.",
            )
        req = urllib.request.Request(
            self.base_url.rstrip("/") + "/chat/completions",
            data=encoded,
            headers={
                "Authorization": f"Bearer {self.api_key}",
                "Content-Type": "application/json",
            },
        )
        self.calls += 1
        last_failure = None
        for attempt in range(3):
            remaining = self.deadline - time.monotonic() if self.deadline else timeout
            if remaining <= 0:
                raise ServiceError(
                    "request_timeout", "The request took too long. Please retry."
                )
            retry_after = 2**attempt
            try:
                with paced_call(self.deadline):
                    remaining = self.deadline - time.monotonic() if self.deadline else timeout
                    self.attempts += 1
                    with urllib.request.urlopen(
                        req, timeout=min(timeout, remaining)
                    ) as response:
                        data = json.loads(response.read())
                usage = data.get("usage") or {}
                self.input_tokens += usage.get("prompt_tokens", 0)
                self.output_tokens += usage.get("completion_tokens", 0)
                if self.deadline and time.monotonic() >= self.deadline:
                    raise ServiceError(
                        "request_timeout", "The request took too long. Please retry."
                    )
                message = data["choices"][0]["message"]
                if not isinstance(message, dict) or not (
                    message.get("content") or message.get("tool_calls")
                ):
                    raise ValueError("Empty message")
                # Keep provider extensions, including thought signatures, in tool history.
                return message
            except urllib.error.HTTPError as exc:
                last_failure = {
                    "stage": "provider", "http_status": exc.code,
                    "kind": "rate_limit" if exc.code == 429 else "http_error",
                }
                # Google also supplies RetryInfo in the JSON body, rather than a header.
                if exc.code == 429:
                    retry_after = 30
                    try:
                        payload = json.loads(exc.read())
                        errors = payload if isinstance(payload, list) else [payload]
                        for item in errors:
                            for detail in item.get("error", {}).get("details", []):
                                if "retryDelay" in detail:
                                    retry_after = max(
                                        1,
                                        min(
                                            float(detail["retryDelay"].rstrip("s")), 30
                                        ),
                                    )
                    except (ValueError, TypeError, AttributeError):
                        pass
                exc.close()
                log.warning("Model HTTP %s on attempt %s", exc.code, attempt + 1)
                if exc.code in (400, 401, 403, 404):
                    raise ServiceError(
                        "configuration_error",
                        "The model request was rejected. Check the server API key, model, and configuration.",
                        500,
                    ) from exc
                if exc.code != 429 and not 500 <= exc.code < 600:
                    raise ServiceError(
                        "upstream_error",
                        "The model provider rejected the request.",
                        502,
                    ) from exc
                try:
                    retry_after = max(
                        retry_after,
                        min(float((exc.headers or {}).get("Retry-After", 0)), 30),
                    )
                except (TypeError, ValueError):
                    pass
            except (ValueError, KeyError, IndexError, TypeError, AttributeError) as exc:
                raise ServiceError(
                    "invalid_model_response",
                    "The model returned an invalid response. Please retry.",
                    502,
                ) from exc
            except (urllib.error.URLError, TimeoutError, OSError) as exc:
                reason = getattr(exc, "reason", exc)
                last_failure = {
                    "stage": "transport",
                    "kind": "timeout" if isinstance(reason, TimeoutError) else "connection_error",
                }
                log.warning(
                    "Model transport %s on attempt %s", type(exc).__name__, attempt + 1
                )
            if attempt < 2:
                if self.deadline and time.monotonic() + retry_after >= self.deadline:
                    break
                time.sleep(retry_after)
        failure = ServiceError(
            "service_unavailable",
            "The model service is temporarily unavailable. Please retry.",
        )
        failure.diagnostics = last_failure or {"stage": "provider"}
        raise failure
