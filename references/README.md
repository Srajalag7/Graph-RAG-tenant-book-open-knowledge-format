# Reference baseline

## Budget-class models

Your service and your local judge runs must use models from this list, or models with equal or lower list price per million tokens. Any hosting (direct API, OpenRouter, a local server) is fine. If a model you want is not listed and is cheaper than the most expensive one here, use it and say so in your write-up.

| Provider | Model |
| --- | --- |
| OpenAI | gpt-4.1-mini, gpt-4.1-nano, gpt-4o-mini, gpt-5-mini, gpt-5-nano |
| Anthropic | claude-haiku-4-5 |
| Google | gemini-2.5-flash, gemini-2.5-flash-lite |
| DeepSeek | deepseek-chat (V3 / V4 flash) |
| Open weights | Llama 3.x 70B and smaller, Qwen 3 32B and smaller, Mistral Small, GLM-4.5-Air, gpt-oss-120b |

Embedding models and rerankers of any size are allowed.

## The baseline pipeline

`references/baseline/run.py`. BM25 over the shipped passages, with two filters applied before retrieval: passages from documents the role may not see are dropped, and passages not in force on the `as_of` date are dropped. The top 6 passages go into one budget-class model with the system prompt in the file. No reranker, no query rewriting, no fine-tuning, no memory between questions.

It is deliberately plain. It exists so the bar is measured against something that actually ran, not against a number we chose.

## Baseline scores on the development split

Run on 2026-09-17 with `gpt-5.6-luna` as both the answering model and the judge, over the 110 development questions, using `grader.py` and `rubric.md` exactly as shipped. The full per-question grade is in `references/baseline_dev_grade.json`.

| Metric | Baseline |
| --- | --- |
| Coverage | 0.72 (n=92) |
| Correctness, pooled | 0.48 (n=66) |
| Correctness, temporal slice | 0.53 (n=15) |
| Correctness, contradiction slice | 0.00 (n=11) |
| Correctness, interpretation slice | 0.48 (n=25) |
| Correctness, numerical slice | 0.80 (n=15) |
| Verbatim citation rate | 0.98 (n=113) |
| Citation support rate | 0.50 (n=110) |
| Out-of-corpus refusal | 1.00 (n=9) |
| Fabricated citations | 0 |
| Role-blocked correct refusal | 0.78 (n=9) |
| Role violations | 0 |
| "No such rule" lies on role-blocked questions | 2 |
| Input tokens per question, mean | 1,905 |
| Output tokens per question, mean | 278 |
| p95 latency, sequential (concurrency 1) | 5.2 s |

Read these as a floor, not a target. The baseline scores zero on the contradiction slice because it never cites the policy book alongside the statute, and it refuses a quarter of answerable questions because BM25 misses passages that use different words from the question. Both are the kind of failure your analysis should name and fix.
