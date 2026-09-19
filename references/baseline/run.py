#!/usr/bin/env python3
"""Reference baseline: BM25 over corpus passages, top-6 into one budget-class model, no reranker.

    python references/baseline/run.py --questions questions/dev.jsonl --out results/baseline_dev.jsonl [--corpus corpus]

Needs MODEL, BASE_URL (OpenAI-compatible /v1) and API_KEY in the environment. Filters passages by
role visibility and by effective dates before retrieval. Everything else is the model's judgment.
This is the pipeline the reference scores in references/README.md come from. It is deliberately
plain. You are expected to beat it.
"""
import argparse
import json
import math
import os
import re
import time
import urllib.error
import urllib.request
from collections import Counter

TOK = re.compile(r"[a-z0-9§\.\-]+")


def tokens(s):
    return TOK.findall(s.lower())


class BM25:
    def __init__(self, docs, k1=1.5, b=0.75):
        self.docs = [tokens(d) for d in docs]
        self.k1, self.b = k1, b
        self.avg = sum(map(len, self.docs)) / max(1, len(self.docs))
        self.df = Counter(t for d in self.docs for t in set(d))
        self.n = len(self.docs)

    def score(self, q, i):
        d = self.docs[i]
        tf = Counter(d)
        s = 0.0
        for t in tokens(q):
            if t not in tf:
                continue
            idf = math.log(1 + (self.n - self.df[t] + 0.5) / (self.df[t] + 0.5))
            s += idf * tf[t] * (self.k1 + 1) / (tf[t] + self.k1 * (1 - self.b + self.b * len(d) / self.avg))
        return s


SYSTEM = """You answer New York housing questions using ONLY the passages provided. Rules:
- If the passages answer the question, reply with status "answered", a plain-language answer, and 1-3 citations. Each citation's "quote" must be copied EXACTLY, character for character, from one passage (a contiguous span of 20-400 characters). Never alter a quote.
- If the passages do not contain the answer, reply with status "refused" and refusal_reason "out_of_corpus".
- If the system message tells you the requester is not permitted to see a relevant document, reply "refused" with refusal_reason "not_permitted" and do not describe that document.
- The requester's role and the as_of date are given. Only rely on passages in force on the as_of date (each passage shows its effective dates). If two passages conflict, say which governs and why in "governing".
- Follow the openigloo Assistant Policy Book passages when they are present: they bind the assistant even when the law says more.
Reply with JSON only: {"status": "answered"|"refused", "answer": str|null, "refusal_reason": "out_of_corpus"|"not_permitted"|null, "refusal_message": str|null, "citations": [{"doc_id": str, "section": str, "quote": str}], "governing": {"doc_id": str, "reason": str, "overridden": [str]}|null, "superseded_notice": str|null}"""


def call(model, base, key, messages):
    body = json.dumps({"model": model, "response_format": {"type": "json_object"},
                       "messages": messages}).encode()
    req = urllib.request.Request(base.rstrip("/") + "/chat/completions", data=body, headers={
        "Authorization": "Bearer " + key, "Content-Type": "application/json"})
    t0 = time.time()
    try:
        r = json.load(urllib.request.urlopen(req, timeout=120))
    except urllib.error.HTTPError as e:
        raise SystemExit(f"model API error {e.code}: {e.read().decode(errors='replace')[:500]}")
    usage = r.get("usage", {})
    return r["choices"][0]["message"]["content"], usage, time.time() - t0


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--questions", required=True)
    ap.add_argument("--out", required=True)
    ap.add_argument("--corpus", default=os.environ.get("CORPUS_DIR", "corpus"))
    ap.add_argument("--k", type=int, default=6)
    ap.add_argument("--limit", type=int, default=0)
    args = ap.parse_args()
    model, base, key = os.environ["MODEL"], os.environ.get("BASE_URL", "https://api.openai.com/v1"), os.environ["API_KEY"]

    manifest = {d["doc_id"]: d for d in json.load(open(os.path.join(args.corpus, "manifest.json")))["documents"]}
    passages = [json.loads(l) for l in open(os.path.join(args.corpus, "passages.jsonl")) if l.strip()]
    bm25 = BM25([manifest[p["doc_id"]]["title"] + " " + p["section"] + " " + p["text"] for p in passages])

    questions = [json.loads(l) for l in open(args.questions) if l.strip()]
    if args.limit:
        questions = questions[:args.limit]
    os.makedirs(os.path.dirname(args.out) or ".", exist_ok=True)
    manifest_out = {"model": model, "k": args.k, "per_question": []}
    with open(args.out, "w") as f:
        for q in questions:
            role, as_of = q["role"], q["as_of"]
            scored, blocked_scores = [], []
            for i, p in enumerate(passages):
                s = bm25.score(q["question"], i)
                if s <= 0:
                    continue
                d = manifest[p["doc_id"]]
                if role not in d["visibility"]:
                    blocked_scores.append(s)
                    continue  # role filter at retrieval
                if (p.get("effective_from") or "0000") > as_of:
                    continue
                if p.get("effective_to") and p["effective_to"] < as_of:
                    continue
                scored.append((s, i))
            ranked = sorted(scored, reverse=True)
            top = [passages[i] for _, i in ranked[:args.k]]
            # a blocked passage "exists" for the model only if it would have outranked the visible top result
            blocked_hit = bool(blocked_scores) and max(blocked_scores) > (ranked[0][0] if ranked else 0)
            ctx = "\n\n".join(
                f"[doc_id={p['doc_id']} | section={p['section']} | in force {p.get('effective_from')} to {p.get('effective_to') or 'present'}"
                f"{' | SUPERSEDED BY ' + manifest[p['doc_id']]['superseded_by'] if manifest[p['doc_id']].get('superseded_by') else ''}]\n{p['text']}"
                for p in top)
            sys_msg = SYSTEM
            if blocked_hit:
                sys_msg += "\nNOTE: a document relevant to this question exists but the requester is not permitted to see it. If the visible passages do not answer the question, refuse with not_permitted."
            user = f"Requester role: {role}\nAs of: {as_of}\nQuestion: {q['question']}\n\nPassages:\n{ctx}"
            try:
                content, usage, dt = call(model, base, key, [{"role": "system", "content": sys_msg}, {"role": "user", "content": user}])
                a = json.loads(content)
            except Exception as e:  # noqa
                if "401" in str(e) or "403" in str(e):
                    raise SystemExit(f"auth error from the model API ({e}); check API_KEY / BASE_URL")
                a, usage, dt = {"status": "refused", "refusal_reason": "out_of_corpus", "refusal_message": f"error: {e}", "citations": []}, {}, 0
            a["id"] = q["id"]
            a.setdefault("citations", [])
            if a.get("status") == "refused":
                a["citations"] = []
            f.write(json.dumps(a, ensure_ascii=False) + "\n")
            f.flush()
            manifest_out["per_question"].append({"id": q["id"], "input_tokens": usage.get("prompt_tokens"),
                                                 "output_tokens": usage.get("completion_tokens"), "latency_s": round(dt, 2)})
            print(q["id"], a.get("status"), a.get("refusal_reason"), f"{dt:.1f}s")
    json.dump(manifest_out, open(args.out.replace(".jsonl", "_manifest.json"), "w"), indent=1)


if __name__ == "__main__":
    main()
