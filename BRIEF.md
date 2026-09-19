# Ask the Tenant Book

## Problem

Build an assistant that answers New York renters' questions from a fixed set of housing documents, quotes the exact passage it relied on, refuses when the answer is not there, respects who is allowed to see what, and stays correct after a rule changes.

> "Our assistant told a renter last week that the landlord could ask for two months' deposit. That has been illegal in New York since June 2019. The renter found out from a lawyer."

Renters on openigloo ask the same questions hundreds of times a week. Can the landlord keep my deposit? Who pays the broker fee on this listing? Is my building rent-stabilized? Does my Section 8 voucher have to be accepted? What happens if the heat is off in January?

The answers exist. They are spread across state statutes, city agency guidance, DHCR fact sheets, Rent Guidelines Board orders, and openigloo's own policy book that says what our assistant may and may not tell a renter. Nobody has time to find them twice, and the wrong version of the right document is the most common way to get them wrong.

- **Temporal.** The answer depends on when the lease was signed or the listing was posted. The deposit cap, the broker fee, this year's guideline increase.
- **Contradiction.** Two documents disagree and the answer must say which governs. A city rule stricter than the state's, or openigloo's own policy stricter than both.
- **Refusal.** "Not in the book" when the answer is not there, and "you are not permitted to see this" when it is there but not for you. These are different answers and both must be distinguishable from a real one.
- **Freshness.** A rule changes after you ship. The old answer must change and the old document must stop being cited as current.

## What you are given

Everyone works from the same files. You do not crawl or collect anything.

| File | What it is |
| --- | --- |
| `corpus/` | About 180 passages across 30 documents, chunked, with document ID, section ID, visibility label, effective date and supersession links |
| `corpus/docs/claire_policy.md` | openigloo's internal assistant policy book, part of the corpus |
| `questions/dev.jsonl` | Development questions with gold answers, gold citations and slice labels |
| `questions/heldout.jsonl` | Held-out questions without answers. We score these |
| `grader.py` and `rubric.md` | The scorer we run and the judge prompts it uses, verbatim |
| `schemas/` | The request and response JSON schemas |
| `freshness_contract.md` | How the updated corpus is supplied after submission and how your service must restart |
| `references/README.md` | Our baseline pipeline and its scores on the development split |

**Not given:** the held-out gold answers and the rule-change pack. We run those ourselves.

### The regulator layer

Public New York housing rules, chunked by us. Included: the Housing Stability and Tenant Protection Act of 2019, the FARE Act of 2025, the Good Cause Eviction law of 2024, the NYC Housing Maintenance Code heat and hot water rules, DHCR fact sheets on rent stabilization, renewals, overcharges, preferential rents and succession, the NYC Human Rights Law provisions on source of income, and the last three Rent Guidelines Board orders.

Some of these documents supersede others. Some are in force only from a date. Some disagree. The corpus manifest records all of this. Whether your system uses it is up to you.

### The internal layer

`claire_policy.md` is openigloo's assistant playbook. It is stricter than the law in places: the assistant never states a specific rent increase figure, never characterizes a landlord as good or bad, and always routes a voucher discrimination report to a human. It is silent in others. Some questions turn on noticing that the internal rule binds first.

### Roles

Every question carries a requester role: `renter`, `landlord` or `staff`. Every document carries a visibility label. Landlord screening notes, internal listing fields and staff escalation procedures are invisible to renters.

A document the requester may not see must never be quoted, paraphrased, summarized, or named as the reason for an answer.

### Questions

Each question has an ID, the question text, a role and an as-of date. Development questions also carry a slice label: `interpretation`, `numerical`, `temporal`, `contradiction`, `out_of_corpus` or `role_blocked`. The held-out set has the same mix in roughly the same proportions.

### Freshness

After scoring your submission we apply a synthetic rule-change pack: a new Rent Guidelines Board order superseding last year's, an amendment to one FARE Act provision, and the withdrawal of one paragraph of `claire_policy.md`. We then restart your service on the updated corpus, following `freshness_contract.md`, and re-run the questions listed in `questions/freshness_subset.jsonl`. The `as_of` date selects the legal rule in force; the policy book always applies as it currently stands. There is no hidden integration.

## What you build

Three parts. All three are graded.

### 1. The service

A `POST /ask` endpoint. Request: `{id, question, role, as_of}`. Response: an answer with one or more citations, each carrying a document ID, a section ID and a verbatim supporting quote; or an explicit refusal with a reason of `out_of_corpus` or `not_permitted`. Exact shapes are in `schemas/`.

- Budget-class models only (the list is in `references/README.md`). Any retrieval stack, index or reranker.
- It must come up from `scripts/reproduce.sh` on a plain Linux box with no GPU, and produce `results/answers.jsonl` over the held-out questions plus `results/manifest.json` with token counts and latency.
- It must restart on an updated corpus following `freshness_contract.md`.

### 2. The frontend

A web interface a renter, a landlord and an openigloo staff member could actually use. Any framework. It talks to your service and nothing else.

It must let the user:

- Ask a question, choose a role, and set the as-of date.
- Read the answer next to its evidence. Each citation opens the source document with the quoted span highlighted in place, not in a separate summary.
- See refusals as refusals. "Not in the book" and "you are not permitted to see this" must look different from each other, and neither may look like an answer.
- See what happened when documents conflict: which document won and why, on the screen, not only in the JSON.
- See document versions. When a citation points to a document that has a newer version in the corpus, the interface says so.

We are not grading visual polish. We are grading whether a renter who does not know what a citation is would trust the right answers and distrust the wrong ones. A screen that turns a refusal into a confident-looking card fails this part.

### 3. The analysis and the memo

A written failure analysis and a two-page memo, described under Required analysis below.

## How we score it

Everything is measured on the held-out split by the published `grader.py`. Nothing is graded from a claim your system makes about itself.

The reference scores below come from a pipeline we built and ran ourselves: BM25 over the shipped passages, the top 6 passages into one budget-class model, no reranker, no fine-tuning. Its full numbers are in `references/README.md`, so you can see the bar and the baseline together.

### Qualification bars

1. **Coverage ≥ 0.85.** In-corpus questions answered rather than refused. A system that refuses everything scores zero here, which is the point.
2. **Answer correctness ≥ 0.75 pooled, and ≥ 0.70 on each of the temporal and contradiction slices** under the published rubric. Both slices must clear their bar, not just the pooled number.
3. **Citation integrity.** Quotes appear verbatim in the cited document at ≥ 0.92 (matched after Unicode normalization, soft-hyphen removal, whitespace collapse and case folding) and support the answer under the rubric at ≥ 0.82. Checked on every citation, not a sample.
4. **Correct refusal on role-blocked questions ≥ 0.87.** The right answer is "you are not permitted to see this", not "there is no such rule". The second is a lie. A renter told that no screening policy exists will act as though none does.
5. **Freshness.** On the post-update subset, answers change where the supplied rule changed, and no superseded document is cited as current.
6. **Frontend fidelity.** On a scripted walkthrough of 12 questions (a plain answer, a contradiction, both refusal types, a superseded citation, and the same question under two roles) the screen shows the state the service returned. Any case where the screen implies more certainty than the JSON carries fails this bar.

## Decisions that are yours

We do not prescribe an architecture. These are the calls we expect you to make and explain:

- How the corpus is chunked, indexed and versioned, and how "in force on date X" is represented at all.
- How contradictions are resolved and surfaced: pick a winner, present both, or escalate to a human.
- Where the refuse-or-answer threshold sits and what evidence sets it. That trade-off is the product.
- Whether role filtering happens at retrieval, at generation, or both, and how you show a leak is impossible rather than unlikely.
- What the frontend shows a renter who asks a question that only a lawyer should answer.
- What re-indexing after an amendment costs, and whether an operations team could run it without you.

## Required analysis

### ANALYSIS.md

- A failure taxonomy of your first working version, and which intervention moved which category of failure.
- The temporal and contradiction slices analyzed separately. Pooled numbers hide exactly the failures this problem exists to catch.
- What broke under your own simulated rule change and why. You do not have our pack, so build one of your own and report on it.
- One honest paragraph on what your system does with a document type it has never seen, such as a court decision or a landlord's own lease rider.

### MEMO.md

Two pages at most, written for the person who will own this assistant at openigloo and who does not read code. It must say:

- What the assistant answers and what it refuses.
- What a wrong answer would cost openigloo, and what it would cost the renter.
- How the team would know the assistant had gone stale.
- What keeping it current for a year takes, in people, time and money.

## Submission and what happens after

### What to send

A private git repository with real history. We read the commits. It must contain:

- `scripts/reproduce.sh` that builds the index, starts the service, runs the held-out questions and writes `results/`.
- The frontend, with one command to start it against the running service.
- `results/answers.jsonl` and `results/manifest.json` from your own final run.
