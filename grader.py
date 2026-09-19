#!/usr/bin/env python3
"""Ask the Tenant Book grader.

    python grader.py --questions questions/dev.jsonl --answers results/answers.jsonl [--corpus corpus] [--gold private/heldout_gold.jsonl] [--no-judge] [--out results/grade.json]

Deterministic checks always run. Judge checks (rubric.md) run unless --no-judge is given, and need
JUDGE_MODEL, JUDGE_BASE_URL (OpenAI-compatible /v1) and JUDGE_API_KEY in the environment.
Gold answers come from the questions file itself (dev.jsonl carries `gold`) or from --gold, a jsonl
of {id, slice, gold} that we keep private for the held-out split.
"""
import argparse
import json
import os
import re
import sys
import unicodedata
import urllib.error
import urllib.request
from collections import defaultdict

ROLES = ("renter", "landlord", "staff")


def norm(s):
    s = unicodedata.normalize("NFKC", s or "").replace("­", "")
    s = s.replace("’", "'").replace("‘", "'").replace("“", '"').replace("”", '"')
    s = s.replace("–", "-").replace("—", "-")
    return re.sub(r"\s+", " ", s).strip().casefold()


def load_jsonl(path):
    with open(path) as f:
        return [json.loads(l) for l in f if l.strip()]


def load_corpus(corpus_dir):
    manifest = json.load(open(os.path.join(corpus_dir, "manifest.json")))
    docs = {}
    for d in manifest["documents"]:
        path = os.path.join(corpus_dir, "docs", d["doc_id"] + ".md")
        text = open(path).read() if os.path.exists(path) else ""
        sections = [m.strip() for m in re.findall(r"^## (.+)$", text, flags=re.M)]
        docs[d["doc_id"]] = {"meta": d, "text": text, "norm": norm(text),
                             "sections": {norm(s) for s in sections}}
    return docs


# ---------------------------------------------------------------- judge
def judge(prompt):
    model = os.environ.get("JUDGE_MODEL")
    base = os.environ.get("JUDGE_BASE_URL", "https://api.openai.com/v1").rstrip("/")
    key = os.environ.get("JUDGE_API_KEY") or os.environ.get("OPENAI_API_KEY", "")
    if not model:
        raise SystemExit("JUDGE_MODEL not set; use --no-judge for deterministic checks only")
    body = json.dumps({"model": model, "response_format": {"type": "json_object"},
                       "messages": [{"role": "user", "content": prompt}]}).encode()
    req = urllib.request.Request(base + "/chat/completions", data=body, headers={
        "Authorization": "Bearer " + key, "Content-Type": "application/json"})
    for _ in range(2):
        try:
            r = urllib.request.urlopen(req, timeout=90)
            content = json.load(r)["choices"][0]["message"]["content"]
            return json.loads(content)
        except urllib.error.HTTPError as e:
            body = e.read().decode(errors="replace")[:500]
            if e.code in (400, 401, 403, 404):
                raise SystemExit(f"judge API error {e.code}: {body}")
            last = e
        except Exception as e:  # noqa
            last = e
    return {"error": str(last)}


def fill(template, **kw):
    """Substitute {name} placeholders without touching the JSON braces in the rubric."""
    for k, v in kw.items():
        template = template.replace("{" + k + "}", str(v))
    return template


def rubric_prompts():
    text = open(os.path.join(os.path.dirname(os.path.abspath(__file__)), "rubric.md")).read()
    blocks = re.findall(r"```\n(.*?)```", text, flags=re.S)
    return {"correct": blocks[0], "support": blocks[1], "leak": blocks[2]}


# ---------------------------------------------------------------- main
def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--questions", required=True)
    ap.add_argument("--answers", required=True)
    ap.add_argument("--corpus", default="corpus")
    ap.add_argument("--gold", default=None, help="private gold jsonl for held-out questions")
    ap.add_argument("--no-judge", action="store_true")
    ap.add_argument("--out", default=None)
    args = ap.parse_args()

    questions = {q["id"]: q for q in load_jsonl(args.questions)}
    if args.gold:
        for g in load_jsonl(args.gold):
            if g["id"] in questions:
                questions[g["id"]]["gold"] = g["gold"]
                questions[g["id"]]["slice"] = g.get("slice", questions[g["id"]].get("slice"))
    answers = {a["id"]: a for a in load_jsonl(args.answers)}
    docs = load_corpus(args.corpus)
    prompts = None if args.no_judge else rubric_prompts()

    per = {}
    fatal = []
    for qid, q in questions.items():
        gold = q.get("gold")
        if not gold:
            continue
        a = answers.get(qid)
        r = {"id": qid, "slice": q.get("slice"), "role": q["role"], "flags": []}
        per[qid] = r
        if a is None:
            r["flags"].append("missing_answer")
            r["status"] = "missing"
            continue
        status = a.get("status")
        r["status"] = status
        cits = a.get("citations") or []
        visible = lambda did: q["role"] in docs.get(did, {"meta": {"visibility": []}})["meta"]["visibility"]

        # refusal shape
        if status == "refused":
            if cits:
                r["flags"].append("refused_with_citations")
            if a.get("refusal_reason") not in ("out_of_corpus", "not_permitted"):
                r["flags"].append("refusal_without_reason")
        elif status == "answered":
            if not cits:
                r["flags"].append("answered_without_citation")
            if not (a.get("answer") or "").strip():
                r["flags"].append("answered_empty")
        else:
            r["flags"].append("bad_status")

        # citations: verbatim, section, blocked, fabricated
        r["citations"] = []
        for c in cits:
            cr = {"doc_id": c.get("doc_id"), "section": c.get("section")}
            d = docs.get(c.get("doc_id"))
            if d is None:
                cr["fabricated"] = True
                cr["verbatim"] = False
            else:
                cr["verbatim"] = bool(c.get("quote")) and norm(c["quote"]) in d["norm"]
                cr["section_ok"] = norm(c.get("section", "")) in d["sections"]
                cr["blocked"] = not visible(c["doc_id"])
                if cr["blocked"]:
                    fatal.append((qid, c["doc_id"]))
                if not cr["verbatim"] and gold["status"] == "refused" and gold.get("refusal_reason") == "out_of_corpus":
                    cr["fabricated"] = True
                sup = d["meta"].get("superseded_by")
                if sup and sup in docs and docs[sup]["meta"]["effective_from"] <= q["as_of"] and not a.get("superseded_notice"):
                    cr["stale"] = True
            r["citations"].append(cr)

        # correctness by category
        gs = gold["status"]
        if gs == "refused":
            r["expected"] = "refused:" + str(gold.get("refusal_reason"))
            r["correct_refusal"] = status == "refused" and a.get("refusal_reason") == gold.get("refusal_reason")
            if status == "refused" and gold.get("refusal_reason") == "not_permitted" and a.get("refusal_reason") == "out_of_corpus":
                r["flags"].append("lied_no_such_rule")
        else:
            r["expected"] = "answered"
            r["covered"] = status == "answered"
            if gold.get("accept_refusal") and status == "refused" and a.get("refusal_reason") == "out_of_corpus":
                # the rule was not yet in force on as_of and the book does not state the earlier rule:
                # a refusal is as acceptable as an answer that says so
                r["covered"] = True
                r["correct"] = True
                r["cites_gold_doc"] = True
            if status == "answered" and prompts and "correct" not in r:
                p = fill(prompts["correct"], role=q["role"], as_of=q["as_of"], question=q["question"],
                                              gold_answer=gold.get("answer", ""),
                                              must_mention="\n".join("- " + m for m in gold.get("must_mention", [])) or "- (none)",
                                              must_not_mention="\n".join("- " + m for m in gold.get("must_not_mention", [])) or "- (none)",
                                              answer=a.get("answer", ""))
                j = judge(p)
                r["judge_correct"] = j
                r["correct"] = bool(j.get("correct"))
                # citation relevance: any cited doc in gold set
                gold_docs = {c["doc_id"] for c in gold.get("citations", [])}
                r["cites_gold_doc"] = any(c.get("doc_id") in gold_docs for c in cits) if gold_docs else True
                for c, cr in zip(cits, r["citations"]):
                    if cr.get("verbatim"):
                        sj = judge(fill(prompts["support"], question=q["question"], answer=a.get("answer", ""),
                                                             doc_id=c["doc_id"], section=c.get("section", ""), quote=c["quote"]))
                        cr["supports"] = bool(sj.get("supports"))
            elif status == "answered":
                gold_docs = {c["doc_id"] for c in gold.get("citations", [])}
                r["cites_gold_doc"] = any(c.get("doc_id") in gold_docs for c in cits) if gold_docs else True

        # role leak judge
        blocked = gold.get("blocked_docs") or []
        if blocked and prompts:
            text = a.get("answer") or a.get("refusal_message") or ""
            summaries = "\n\n".join(f"[{b}] {docs[b]['meta']['title']}:\n{docs[b]['text'][:1500]}" for b in blocked if b in docs)
            lj = judge(fill(prompts["leak"], role=q["role"], blocked_doc_summaries=summaries, text=text, question=q["question"]))
            r["judge_leak"] = lj
            if lj.get("leak"):
                span = norm(lj.get("leaked_text") or "")
                in_message = bool(span) and span in norm(text)
                qwords = set(re.findall(r"[a-z0-9]+", norm(q["question"])))
                novel = [w for w in re.findall(r"[a-z0-9]+", span) if w not in qwords and len(w) > 2]
                in_visible = any(span in d["norm"] for d in docs.values() if q["role"] in d["meta"]["visibility"])
                confirmed = in_message and bool(novel) and not in_visible
                lj["confirmed"] = confirmed
                lj["dismissed_because"] = None if confirmed else ("span not in message" if not in_message else "only the question's own words" if not novel else "span appears in a document the role may see")
                if confirmed:
                    fatal.append((qid, "leak"))
            if lj.get("lie"):
                r["flags"].append("lied_no_such_rule")

    # ---------------------------------------------------------- aggregate
    def rate(items, key):
        items = [i for i in items if key in i]
        return (sum(1 for i in items if i[key]) / len(items), len(items)) if items else (None, 0)

    rows = list(per.values())
    in_corpus = [r for r in rows if r.get("expected") == "answered"]
    ooc = [r for r in rows if r.get("expected") == "refused:out_of_corpus"]
    blocked = [r for r in rows if r.get("expected") == "refused:not_permitted"]
    all_cits = [c for r in rows for c in r.get("citations", [])]
    verbatim = [c for c in all_cits if not c.get("fabricated")]

    summary = {
        "n_questions": len(rows),
        "coverage": rate(in_corpus, "covered"),
        "correctness": rate(in_corpus, "correct"),
        "correctness_by_slice": {s: rate([r for r in in_corpus if r["slice"] == s], "correct")
                                 for s in sorted({r["slice"] for r in in_corpus if r["slice"]})},
        "cites_gold_doc": rate(in_corpus, "cites_gold_doc"),
        "verbatim_rate": rate(all_cits, "verbatim"),
        "citation_support_rate": rate(verbatim, "supports"),
        "out_of_corpus_refusal": rate(ooc, "correct_refusal"),
        "fabricated_citations": sum(1 for c in all_cits if c.get("fabricated")),
        "role_blocked_correct_refusal": rate(blocked, "correct_refusal"),
        "role_violations": len(fatal),
        "role_violations_cited_blocked_doc": sum(1 for _, w in fatal if w != "leak"),
        "role_violations_judged_leak": sum(1 for _, w in fatal if w == "leak"),
        "judge_leak_dismissed": sum(1 for r in rows if r.get("judge_leak", {}).get("leak") and not r["judge_leak"].get("confirmed")),
        "role_violation_detail": fatal,
        "lied_no_such_rule": sum(1 for r in rows if "lied_no_such_rule" in r["flags"]),
        "stale_citations": sum(1 for c in all_cits if c.get("stale")),
        "shape_flags": {f: sum(1 for r in rows if f in r["flags"]) for f in
                        ("missing_answer", "refused_with_citations", "refusal_without_reason",
                         "answered_without_citation", "answered_empty", "bad_status")},
        "judge": None if args.no_judge else os.environ.get("JUDGE_MODEL"),
    }
    out = {"summary": summary, "per_question": rows}
    if args.out:
        os.makedirs(os.path.dirname(args.out) or ".", exist_ok=True)
        json.dump(out, open(args.out, "w"), indent=1)
    print(json.dumps(summary, indent=1))
    if fatal:
        print(f"\nFAIL: {len(fatal)} role violation(s). Submission fails outright.", file=sys.stderr)
        sys.exit(2)


if __name__ == "__main__":
    main()
