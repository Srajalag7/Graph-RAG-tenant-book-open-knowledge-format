# Memo: Operating the Tenant Book Assistant

Operations, capabilities, and 1-year cost estimate

## Assistant Capabilities and Refusals
I have developed a housing rules assistant that accurately answers renter queries by strictly quoting from the allowed policy documents. The assistant is designed to handle complex edge cases deterministically:
- **Direct Answers & Contradictions**: It answers questions when rules are clearly stated. If two rules conflict (e.g., city law vs. state law, or openigloo policy vs. general law), the system explicitly identifies the conflict, names the controlling rule, and provides verbatim quotes.
- **Temporal Awareness**: It answers questions based on the specific `as_of` date provided. A rule valid in 2023 will not be incorrectly applied to a 2025 query. 
- **"Not in the book" (Out of Corpus)**: If a renter asks a question whose answer does not exist in the provided corpus, the assistant explicitly refuses to answer rather than guessing or hallucinating an answer.
- **"You are not permitted to see this" (Role-Blocked)**: If a renter asks about an internal policy or a landlord-only screening note, the system will explicitly state they are not permitted to see it. It will **not** lie and say "the rule doesn't exist", as that could mislead a renter into thinking no screening policy exists at all.

This strict retrieval and explicit refusal approach prevents hallucinations, ensures we don't dispense incorrect legal advice, and builds trust by being transparent about what the assistant can and cannot see.
## The Cost of Incorrect Answers
A wrong answer can be highly damaging. If a renter acts on an outdated or incorrect rule (e.g., paying an unlawful fee), openigloo faces loss of trust, increased support workload, and potential legal exposure. For openigloo, the cost of a single major incident far outweighs the annual running cost of the LLM. 

## Detecting Staleness and Managing Updates
To prevent staleness, the system relies on an Open Knowledge Format (OKF) index. 
If housing laws change, or if we need to add a new document (such as a new evaluation letter, a recent Rent Guidelines Board order, or a modified openigloo policy), the update process is straightforward:
1. **Add the Document**: An operations team member drops the new markdown file (e.g., the evaluation letter) into the `corpus/docs/` directory.
2. **Rebuild the Graph**: The team runs the `reindex.sh` rebuild script. This takes under 15 minutes and automatically maps the new document into the knowledge graph, establishing its topics and relationships to older rules.
3. **Evaluate the Update**: Before deploying, the team can run a freshness evaluation (e.g., using `questions/freshness_subset.jsonl`) to ensure the assistant correctly applies the newly added letter to current queries.

The system will automatically serve the updated rules from the new letter for current queries while retaining the historical context of the older rules for past dates.

## 1-Year Operating Estimate
This estimate assumes **100 queries per day** (36,500 questions/year), using a budget-class model like Gemini 3.5 Flash-Lite or an OpenAI equivalent. I have excluded staffing costs, focusing purely on infrastructure and API expenses.

| Item | Annual Estimate |
| --- | ---: |
| Answering Model API (100 queries/day) | $571 |
| Weekly OKF Rebuild API Allowance | $10 |
| Evaluation / Diagnostics Model Allowance | $50 |
| Small Application Server ($20/month) | $240 |
| **Total Annual Cost** | **$871** |
| Total with 20% Contingency | **$1,045** |

At roughly $1,045 a year, the assistant is highly cost-effective to operate.
