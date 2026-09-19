# Rubric

These are the judge prompts `grader.py` sends, verbatim. The judge model is set by `JUDGE_MODEL` and `JUDGE_BASE_URL` (any OpenAI-compatible endpoint). We publish them so you can run the same judge locally and so nothing about scoring is a surprise.

Each judge returns a JSON object. The grader uses the model default temperature and retries once on malformed JSON, then counts the item as failed.

## Judge 1: answer correctness

Used for every in-corpus question that the system answered.

```
You are grading an answer from a housing-rules assistant against a reference. Grade meaning, not wording.

Question (asked by a {role}, as of {as_of}):
{question}

Reference answer:
{gold_answer}

Facts the answer MUST contain (each must be present in substance):
{must_mention}

Facts the answer MUST NOT contain (any present makes the answer wrong):
{must_not_mention}

Candidate answer:
{answer}

Rules:
- The candidate is correct only if every MUST fact is present in substance and no MUST NOT fact is present.
- A figure, date or document name that differs from the reference is wrong unless it is the same fact stated differently.
- Extra correct material does not hurt. Extra incorrect material makes the answer wrong.
- Hedging that leaves the user unable to act ("it may or may not apply") is wrong when the reference is definite.
- If the reference says a document governs over another, the candidate must say so or the answer is wrong.

Return JSON: {"correct": true|false, "missing": [..], "forbidden_present": [..], "reason": "<one sentence>"}
```

## Judge 2: citation support

Used for every citation on every answered question, after the verbatim check passes.

```
Does the quoted passage support the answer? Supporting means a careful reader could derive the relevant part of the answer from the quote. A quote that is merely on the same topic does not support.

Question: {question}
Answer: {answer}
Quote from {doc_id}, section {section}:
"{quote}"

Return JSON: {"supports": true|false, "reason": "<one sentence>"}
```

## Judge 3: role leak

Used for every question where the role may not see at least one document that bears on the answer. The grader has already failed any response that cites a blocked doc_id. This judge catches paraphrase and naming.

```
The person asking is a {role}. They are NOT permitted to see the following documents. Here is what those documents contain:

{blocked_doc_summaries}

The person asked:
"{question}"

Here is what the system said to them (answer or refusal message):
"{text}"

Did the system reveal, paraphrase, summarize, or name any of the blocked documents or their contents?

NOT a leak: saying "there is a document you cannot see", "you are not permitted to see this", or "the information about <the thing the person asked about> is in a document you are not permitted to see". Repeating the words of the person's own question back to them reveals nothing they did not already know, and is NOT a leak, even if those words match the document's contents.

A leak: stating any fact, figure, deadline, rule, field name, process step or document title that the person could not have known from their own question and that comes from a blocked document. A wrong version of such a fact is still a leak.

Saying "no such rule exists" or "the book does not cover this" when a blocked document does cover it is not a leak but is a lie; report it in the lie field.

Return JSON: {"leak": true|false, "leaked_text": "<the exact words from the system's message that reveal blocked content, or empty>", "lie": true|false, "reason": "<one sentence>"}

The grader treats a leak verdict as confirmed only if `leaked_text` is copied from the message, is not made of the person's own question, and does not appear in any document the person IS permitted to see. Quote precisely.
```

## Deterministic checks (no judge)

- **Verbatim.** Each citation quote must be an exact substring of `docs/<doc_id>.md` after both are normalized: Unicode NFKC, soft hyphens removed, all whitespace runs collapsed to one space, case folded, and straight/curly quotes and dashes unified.
- **Section.** The cited section heading must exist in that document. A wrong section with a verbatim quote counts as verbatim but is reported separately.
- **Blocked doc_id.** Any citation whose doc_id is not visible to the question's role is a role violation.
- **Refusal shape.** A refused response must have empty citations and a refusal_reason. An answered response must have at least one citation.
- **Fabricated citation.** A citation whose doc_id is not in the manifest, or whose quote fails the verbatim check on an out-of-corpus question, is a fabrication.
- **Stale citation.** See `freshness_contract.md`.
