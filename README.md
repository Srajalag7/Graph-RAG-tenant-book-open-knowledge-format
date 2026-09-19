# Ask the Tenant Book

The brief is in `BRIEF.md`. This file is the map of the package.

| Path | What it is |
| --- | --- |
| `BRIEF.md` | The problem, what you build, how it is scored |
| `corpus/manifest.json` | Every document: id, title, issuer, layer, visibility by role, effective dates, supersession links, source URL |
| `corpus/passages.jsonl` | The chunked corpus. One passage per line with document id, section, text and effective dates |
| `corpus/docs/<doc_id>.md` | Full text of each document with `##` section headings. Citation quotes are checked against these files |
| `questions/dev.jsonl` | Development questions with gold answers and slice labels |
| `questions/heldout.jsonl` | Held-out questions. No gold. We score these |
| `questions/freshness_subset.jsonl` | The fixed list of questions we re-run after the rule-change pack. Questions only; some are new and refer to documents that exist only in the pack |
| `grader.py` | The scorer we run. Run it yourself on the dev split |
| `rubric.md` | The judge prompts, verbatim, and the deterministic checks |
| `schemas/` | Request, response and question JSON schemas |
| `freshness_contract.md` | How the updated corpus is supplied after submission and what your service must do |
| `references/README.md` | The baseline pipeline, the allowed model list, and the baseline's scores |
| `references/baseline/run.py` | The baseline itself |
| `scripts/build_corpus.py` | How `manifest.json` and `passages.jsonl` were produced from `corpus/parts/`. For transparency only |

## Running the grader

```
export JUDGE_MODEL=<a model from references/README.md>
export JUDGE_BASE_URL=https://api.openai.com/v1   # any OpenAI-compatible endpoint
export JUDGE_API_KEY=...
python grader.py --questions questions/dev.jsonl --answers results/answers.jsonl --out results/grade.json
```

`--no-judge` runs only the deterministic checks (verbatim quotes, sections, role visibility, refusal shape, stale citations) and needs no API key.

## Corpus notes

The regulator layer is public New York State and New York City law and agency guidance, fetched from official sources and chunked by us. Source URLs are in the manifest. Text was cleaned for extraction artifacts only. Where a document has been amended, the manifest `notes` field says what changed and when; the corpus contains the current text unless the manifest says otherwise.

The internal layer (`issuer: openigloo`) is synthetic. It describes rules for a fictional version of openigloo's assistant and does not reflect openigloo's actual internal policies.
