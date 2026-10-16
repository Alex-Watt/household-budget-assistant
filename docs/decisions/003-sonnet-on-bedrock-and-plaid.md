# Claude Sonnet on Bedrock for questions, Plaid for card data

[Back to README](../../README.md) · [Banking and AI setup](../banking-and-ai-setup.md) · [Roadmap](../roadmap.md)

**Status:** Accepted as the design. This public sample ships with Plaid and AI off.  
**Date:** 13 October 2026

## Context

The Discord channel is built. What remained was real card data (Milestones 2 and 3) and natural-language answers (Milestone 4). The household asked for the AI to use the **highest version of Claude Sonnet**, which is 5.5 today and will change.

- **Bedrock can list a newer Sonnet** that the account still cannot call. AWS documents "not available for this account" as an account-level eligibility limit, not something code can fix. The adapter therefore tries the highest Sonnet and falls back to the next one that works.
- **Plaid** supports Chase credit cards. The sample's Robinhood Gold Card is reachable through Plaid for budgeting apps, but community reports say Plaid does not expose its *liabilities* data, only transactions, which is all this app needs. The Robinhood app also exports a CSV. For new US teams, Plaid's **Trial plan** is free for up to 10 live Items, includes Transactions, and gives immediate Chase access.
- Plaid's current category set (PFCv2) is the only one new accounts receive. Its extra subcategories are in income, loans, fees and transfers, none of which count as spending, so the mapping keys on primary categories.

## Decision

**Model.** Call Claude through the Amazon Bedrock Converse API from the worker Lambda, with IAM credentials and no API key to store.

- The adapter lists the account's Sonnet inference profiles, sorts by version, and uses the first that works. A newer Sonnet is picked up with no code change. If the newest is not enabled for the account, it falls to the next highest and remembers the working one. The list refreshes every 30 minutes, so fixing access takes effect without a redeploy. It only ever picks Sonnet, never a higher-numbered Opus, Haiku or other family.
- Profiles use the `us.` prefix by default so traffic stays in the United States. `AI_PROFILE_SCOPE=global` is available.
- **AI is off by default** (`AI_ENABLED=false`). With it off, the bot still answers `summary`, category names, `coverage` and `help` exactly, using no model. The IAM permission to call Bedrock is only created when AI is on.
- The model never produces a figure. It chooses among seventeen restricted tools, each a deterministic Python function. After it answers, every dollar amount and percentage must appear verbatim in the tool results, or the answer is replaced by the plain Python summary.
- The daily 8 AM report never uses the model, so a model outage cannot block it.
- Category corrections, budget changes (one month or every month from now on), charge adjustments and merchant rules are the only writes. The question must open with a request ("move ...", "please undo ..."), contain no negation, and name the target category; the transaction must come from a search in the same conversation; only one change is allowed per question and per Discord interaction (a redelivered request replays the stored result); it is audited with the Discord user ID and reversible, and `undo` walks back through earlier corrections one at a time. Merchant text, which could contain hostile instructions, is cleaned of link-building characters, bounded and handed to the model only inside quoted JSON. The worker's ledger write permission is limited to the correction, audit, replay-mark, budget, adjustment, merchant-rule and standing-limit records, and it may delete only an undone correction.
- A budget change (a month's category limits, this month or next only) needs a request-shaped question with a number, and the tool refuses unless every category, amount, month and direction matches what the person wrote. All arithmetic is in code. Afterwards the reply is the program's own description of what was saved, and the model is not asked again, so a change can never be misreported or reported twice. Each month's changes and their undo history sit on one conditionally written record, so concurrent changes cannot overwrite each other.
- **What the AI changes is saved data, never code or formulas.** A charge can be made to count for less than the card shows (a gift that someone paid half of), a standing merchant rule can re-file every matching charge, and a limit can change from now on. The next 8 AM run reads that data, so it reports the adjusted totals; the formulas stay the tested Decimal code. Each of these has its own gate (a statement or request, no doubt or negation), checks every figure, charge and merchant text against the person's words, makes one change per question and tells the person when a second was not made, is audited, versioned and undoable, and is reported in the program's own words. Reviewed twice by an independent Sonnet 5.5 agent and mutation-tested; a live run with the real model caught that the search could not find `WGC*RUTH CHRIS` from "Ruth's Chris", so search and rule matching now ignore apostrophes and symbols.
- Spending is capped at 30 AI answers a day (`AI_DAILY_LIMIT`), four tool rounds with at most two tool calls each, 700 output tokens per call, and the Lambda's real remaining time: a round starts only with at least 55 seconds left of the 90-second worker timeout.
- The worker stores the last question and reply. A follow-up that names a listed charge by date, amount, or “the first” / “the second” finishes that change in Python. A message that is only “yes”, “yeah”, “sure”, or “ok” is sent to the model as acceptance of the offer in the previous reply.

**Rejected:** calling the Anthropic API directly (needs a second secret, sends data outside AWS, separate billing). The household confirmed on 13 October: Bedrock only. The `LlmClient` interface would keep a swap small, but none is planned. OpenAI, from the original brief, is dropped because the household chose Claude.

**Card data.** Plaid Transactions only, read-only, through `/transactions/sync`.

- A local script (`scripts/plaid_link.py`) runs Plaid's Hosted Link, so there is no web app to host. Plaid shows the bank login; this app never sees credentials. Access tokens go straight into Secrets Manager (`household-budget/plaid`) and are never printed.
- A sync Lambda runs at 7:30 AM and 7:30 PM Central, and whenever Plaid's signed webhook says updates are waiting. The webhook receiver verifies Plaid's ES256 signature, a five-minute freshness window and the body hash before doing anything.
- Plaid and its Lambdas are created only when `PLAID_ENABLED=true`.
- **Spend date** is the day a purchase was authorized (else posted), so a purchase made on the 31st and posted on the 1st belongs to the 31st's month. This is an assumption; say so if you prefer statement months.
- Pending and posted are reported separately. Remaining budget counts pending, so it never overstates room.
- Card payments, transfers and income are excluded. Refunds net against their category. The same purchase arriving twice (a duplicated Chase Item, or a CSV overlapping Plaid) is counted once.
- Costco and Target move from Shopping to Groceries and household only when the provider chose Shopping. Costco gas stays Transportation. The provider's original category is always kept, and corrections are stored separately so they can be undone.
- A **CSV import** (`scripts/import_csv.py`) covers a card Plaid cannot read. Rows have stable IDs, so re-importing or overlapping exports never double counts.

## Consequences

- Cost: Bedrock bills per token. A question uses roughly 6,000 to 10,000 input tokens across its tool rounds; check [current Bedrock pricing](https://aws.amazon.com/bedrock/pricing/) before leaving AI on. The daily cap bounds the worst case. Plaid's Trial plan is free for up to 10 Items. A paid Plaid plan bills a monthly subscription for each Item until it is removed.
- The household's merchants and amounts reach Bedrock when a question needs them, and Plaid holds the bank connection. Daily posts to Discord show totals only, never merchant names.
- The worker timeout rose from 30 to 90 seconds, the queue visibility from 180 to 540 seconds and the message lease from 60 to 120 seconds, because a model conversation takes longer than a lookup.
- One more DynamoDB table (the ledger), with point-in-time recovery, retained on stack deletion.
- Optional alarms, an SNS email topic and a $50 monthly cost budget are created only when `ALERT_EMAIL` is set. They alert; they do not stop spending. An account spend limit, when the account has one, is the hard ceiling.

## Open items

1. **Bedrock access.** A newer Sonnet can be listed and still be unavailable. The `us.` profile may also route to Regions a managed account denies. Run `scripts/check_ai_access.py` in the account you deploy to.
2. **Plaid stays off** until you connect your own read-only items. Sandbox rehearsal and update mode are still open. See the [setup guide](../banking-and-ai-setup.md). The sample card names are Chase Sapphire Preferred and Robinhood Gold Card.
3. **Robinhood CSV columns** are not known, so only a Chase preset and a generic format exist.
4. **Still not built:** a button confirmation for category changes (the question-gate, replay guard, undo, and the one-exchange “yes” follow-up are in place), splitting the Plaid credentials from the access tokens so the public webhook Lambda cannot read tokens, and a permissions boundary on the CloudFormation role before any GitHub deploy.
