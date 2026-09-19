#!/usr/bin/env python3
"""Merge corpus/parts/*.manifest.json and *.passages.jsonl into corpus/manifest.json and
corpus/passages.jsonl. Documents that have no passages file (the internal layer) are chunked
here by ## heading. Run from the repo root: python scripts/build_corpus.py [corpus_dir]"""
import glob
import json
import os
import re
import sys
import unicodedata

corpus = sys.argv[1] if len(sys.argv) > 1 else "corpus"
docs_dir = os.path.join(corpus, "docs")
parts = os.path.join(corpus, "parts")

documents, passages, seen_pass = [], [], set()
for mf in sorted(glob.glob(os.path.join(parts, "*.manifest.json"))):
    documents += json.load(open(mf))["documents"]
for pf in sorted(glob.glob(os.path.join(parts, "*.passages.jsonl"))):
    for line in open(pf):
        if line.strip():
            p = json.loads(line)
            seen_pass.add(p["doc_id"])
            passages.append(p)

by_id = {d["doc_id"]: d for d in documents}


def split_words(text, lo=120):
    paras = [p.strip() for p in re.split(r"\n\s*\n", text) if p.strip()]
    chunks, cur = [], []
    for p in paras:
        cur.append(p)
        if len(" ".join(cur).split()) >= lo:
            chunks.append("\n\n".join(cur))
            cur = []
    if cur:
        if chunks and len(" ".join(cur).split()) < 40:
            chunks[-1] += "\n\n" + "\n\n".join(cur)
        else:
            chunks.append("\n\n".join(cur))
    return chunks


for d in documents:
    if d["doc_id"] in seen_pass:
        continue
    path = os.path.join(docs_dir, d["doc_id"] + ".md")
    if not os.path.exists(path):
        print("MISSING doc file for", d["doc_id"])
        continue
    body = re.sub(r"<!--.*?-->", "", open(path).read(), flags=re.S)
    sections = re.split(r"^## ", body, flags=re.M)[1:]
    n = 0
    for sec in sections:
        heading, _, text = sec.partition("\n")
        for chunk in split_words(text.strip()):
            n += 1
            passages.append({
                "passage_id": f"{d['doc_id']}#{n:02d}", "doc_id": d["doc_id"],
                "section": heading.strip(), "text": chunk,
                "effective_from": d["effective_from"], "effective_to": d["effective_to"]})


def norm(s):
    s = unicodedata.normalize("NFKC", s).replace("­", "")
    s = s.replace("’", "'").replace("‘", "'").replace("“", '"').replace("”", '"')
    s = s.replace("–", "-").replace("—", "-")
    return re.sub(r"\s+", " ", s).strip().casefold()


bad = 0
doc_norm = {}
for p in passages:
    path = os.path.join(docs_dir, p["doc_id"] + ".md")
    if p["doc_id"] not in doc_norm:
        doc_norm[p["doc_id"]] = norm(open(path).read()) if os.path.exists(path) else ""
    if norm(p["text"]) not in doc_norm[p["doc_id"]]:
        bad += 1
        print("NOT VERBATIM:", p["passage_id"])
    if p["doc_id"] not in by_id:
        print("passage for unknown doc:", p["passage_id"])

json.dump({"documents": documents, "generated_by": "scripts/build_corpus.py"},
          open(os.path.join(corpus, "manifest.json"), "w"), indent=1)
with open(os.path.join(corpus, "passages.jsonl"), "w") as f:
    for p in passages:
        f.write(json.dumps(p, ensure_ascii=False) + "\n")
print(f"{len(documents)} documents, {len(passages)} passages, {bad} non-verbatim")
