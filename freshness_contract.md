# Freshness contract

After we score your submission we re-run a subset of questions against an updated corpus. This document is the whole integration. There is nothing hidden.

## What changes

We supply a second corpus directory with the same layout as `corpus/`: `manifest.json`, `passages.jsonl`, and `docs/<doc_id>.md`. It differs from the shipped corpus in three ways:

1. **A superseding version.** A new Rent Guidelines Board order appears as a new document. The prior order gains a `superseded_by` value and an `effective_to` date. Questions with an `as_of` inside the new order's period must answer from the new order. Questions with an `as_of` inside the old order's period must still answer from the old one.
2. **An amendment.** One existing document is replaced by a new version of itself: same `doc_id`, `version` incremented, some passage text changed, `effective_from` set to the amendment date. The old text is retained as `docs/<doc_id>@v1.md` with `effective_to` set, and its passages carry `effective_to`.
3. **A withdrawal.** One section of `claire_policy.md` is removed. The manifest entry's `notes` field records which section was withdrawn and when. Questions that previously turned on that section must no longer be governed by it.

**How `as_of` interacts with a policy change.** The `as_of` date selects which *legal* rule was in force. openigloo's policy book governs the assistant's behaviour at the time the question is asked, not historically: once a policy section is withdrawn, the assistant follows the current book for every question, whatever its `as_of`. A question with `as_of` in 2025 that previously turned on the withdrawn section is answered under the current policy after the update.

We may also add up to three questions that are unanswerable from the updated corpus, to confirm the system still refuses correctly after re-indexing.

## What your service must do

- Read the corpus directory from the environment variable `CORPUS_DIR`. Default `./corpus`.
- Provide `scripts/reindex.sh <corpus_dir>` that rebuilds whatever index your service uses from that directory, from scratch, in under 15 minutes on a plain Linux box with no GPU. It may call model APIs (for embeddings, for example). It must not require any file outside `<corpus_dir>` and your repository.
- Provide `scripts/serve.sh` that starts the service against `CORPUS_DIR` and exits non-zero if the index for that directory does not exist.
- Nothing in your service may hard-code a document's content, a figure, a date, or a supersession relationship. The manifest is the only source of those facts. If your system caches anything derived from the corpus, the cache must be keyed on the corpus directory's contents, not on its path.

## What we run

```
export CORPUS_DIR=/path/to/corpus_v2
scripts/reindex.sh "$CORPUS_DIR"
scripts/serve.sh &
python grader.py --questions questions/freshness_subset.jsonl --answers results/answers_v2.jsonl --corpus "$CORPUS_DIR" --gold private/freshness_gold.jsonl
```

## How it is scored

- **Changed where it should.** For each question whose gold answer differs between the two corpora, the new answer must match the new gold under the rubric.
- **Unchanged where it should.** For each question whose gold answer is the same in both corpora, the new answer must still be correct.
- **No stale citation.** No answer may cite a document as current when the manifest marks it `superseded_by` another document whose `effective_from` is on or before the question's `as_of`. Citing the superseded document is acceptable only when the answer is about the period in which it was in force, and the `superseded_notice` field must then be set.
- **No ghost citation.** No answer may cite a section that no longer exists in the updated corpus.

The freshness bar is pass or fail on all four together.
