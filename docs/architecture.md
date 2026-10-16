# Architecture and failure behavior

[Back to README and diagrams](../README.md#infrastructure-and-connections)

## Code boundaries

Money and rules never depend on AWS, Discord, Plaid or a model. Adapters are injected.

| Module | Responsibility |
|---|---|
| `money.py` | Decimal-only cents, parsing, `$1,234.56` formatting, percentages. No floats touch a dollar amount |
| `categories.py` | The 14 categories and their default $6,280 caps (checked at import), Plaid category mapping, the Costco/Target rule, category name matching |
| `accounts.py` | Stable keys for the two cards; no account numbers |
| `transactions.py` | `Transaction`, `Override`, text cleaning, exclusion of payments/transfers/income, pending-to-posted and cross-feed de-duplication, category resolution with provenance (a person's correction, then a household rule, then Costco/Target, then the provider), charge adjustments, and merchant matching that ignores apostrophes and symbols |
| `changes.py` | The household's own changes: charge adjustments ("counts for $100"), standing merchant rules and standing limits, each versioned, audited and undoable |
| `budget.py` | Month summary, pace and projection, category detail, search, "why did it change", purchase check, coverage and freshness, audited corrections, and per-month category limit changes with undo. Central-time and daylight-saving correct |
| `render.py`, `commands.py` | Plain-text rendering of results; the commands answered without a model |
| `ports.py` | Interfaces: reply, post, messaging state, ledger, sync state, transaction provider, responder, daily limit |
| `ledger.py`, `ledger_memory.py` | DynamoDB and in-memory ledgers; the memory one backs tests and the demo and is checked against the DynamoDB one for identical answers |
| `fixtures.py` | Synthetic card data for tests and the demo |
| `plaid.py` | Plaid HTTP client (Decimal JSON, credentials in headers, error codes only), transaction normalization, incremental `/transactions/sync` provider |
| `plaid_webhook.py` | Plaid signature verification (ES256) and the receiver that starts a sync |
| `sync.py` | Pulls a feed into the ledger with a lock, a cursor that only advances after storing, and per-feed freshness; the sync Lambda entry |
| `csv_import.py` | CSV parsing with stable row IDs |
| `llm.py` | Bedrock Converse adapter that finds the highest usable Claude Sonnet |
| `tools.py` | The fifteen restricted tools (six read, nine guarded writes), argument validation, the change-intent gates |
| `assistant.py` | Commands first, then the model loop with limits, time budget, number-grounding check and fallbacks |
| `messaging.py` | Duplicate handling, dry run, handing the question to the responder |
| `report.py` | The 8 AM message from real totals, once per day, Central-time date |
| `discord.py` | Ed25519 request verification, interaction parsing, reply and webhook adapters and mock |
| `config.py` | Validated settings for infrastructure and runtime; no AWS calls |
| `credentials.py` | Secrets Manager lookups: Discord webhook URL, Plaid keys and tokens (validated, hidden from `repr`, cached five minutes) |
| `state.py` | DynamoDB processing leases, completed-record retention, daily AI counter |
| `webhook.py`, `worker.py` | Discord API Gateway and SQS Lambda entry points |
| `infrastructure/stack.py` | AWS resources, IAM grants, schedules, alarms, outputs |

## Question lifecycle

1. Discord posts a signed interaction to `POST /discord`. The handler verifies the signature over the timestamp plus raw bytes and rejects timestamps more than five minutes off, with `401` on failure. `PING` gets `PONG`.
2. The handler requires `/ask` from the configured application, server, channel, and one of the two user IDs. Anyone else gets a private refusal and nothing is queued.
3. SQS stores the interaction ID, user ID, question (at most 500 characters), and the interaction token. In live mode the handler answers with a deferred reply, so Discord shows that the bot is thinking. In dry run it answers with a private notice instead. A queue failure fails the request and Discord shows an error; Discord does not retry.
4. The worker conditionally claims `MSG#<interaction ID>` in DynamoDB. A token owns a 120-second processing lease. A completed record returns `duplicate`; a busy lease causes a retry rather than acknowledgement. After a crash, another worker may reclaim an expired lease.
5. In live mode the worker asks the `Assistant` for the reply (see below) and edits the deferred reply with the interaction token. That needs no AWS secret. Dry runs never read the ledger and never call Discord or the model.
6. Successful processing marks the claim complete. Exceptions release only the caller's claim. SQS retries only failed records and eventually moves them to the dead-letter queue.

## How a question is answered

1. **Commands first.** `help`, `summary` (and phrasings like "how much have we spent this month?"), a category name (and "how much shopping budget is left?"), and `coverage` are answered by Python with no model. They are exact, free and work if AI is off or down.
2. **No card data, no model.** If no card has synced, the reply says so and the model is not called.
3. **Daily cap.** At most `AI_DAILY_LIMIT` AI answers per Central day, counted atomically in DynamoDB.
4. **The model loop.** The worker sends the question and seventeen tool definitions to Claude Sonnet through Bedrock Converse, runs the tools it asks for (at most two per round and four rounds), and returns each result. Calls always set `maxTokens`. A round starts only if the Lambda's real remaining time (read from its context) is at least 55 seconds, which covers one model call, loading the model list, and the Discord edit, so the 90-second worker timeout holds. The last question and reply are stored. A short “yes” is passed to the model as acceptance of the previous offer.
5. **Tools are deterministic.** Each wraps a function in `budget.py`: month summary, category status, transaction search by merchant/category/period, "explain a category change" against the same days last month, purchase check, data coverage, and eleven guarded writes (recategorize a transaction or undo it; change a month's limits, or every month's from now on, or undo that; adjust what a charge counts for or undo it; split a charge or undo it; set, remove or undo a merchant rule). Periods such as "last week" (Monday to Sunday) are resolved in Python, not by the model. Results are JSON whose money values are already formatted strings.
6. **Number grounding.** The answer is normalized (so full-width `＄` and `％` are seen) and every dollar amount and percentage in it is compared by value, whether written `$5`, `$ 5`, `USD 5`, `5 dollars`, `5 bucks`, `5%` or `5 percent`, with the tool results and the question. A figure the model itself supplied, such as a purchase amount passed to `check_purchase`, does not count as evidence. Any mismatch, or any link in the answer, first sends the model one instruction to rewrite without it; if the second try is no better, the plain summary replaces it and the log records `ai_ungrounded`. An over-long answer is cut at a line or sentence end first. This is a safety net, not a proof: amounts spelled out in words ("five hundred dollars") are not caught, which is why the prompt also forbids doing arithmetic.
7. **Failure.** Any model, Bedrock or tool-loop error returns "My AI helper isn't available right now" plus the plain summary. Logs carry the exception type only.

**Model choice.** `llm.py` lists the account's Sonnet inference profiles (default `us.` scope), orders them by version, and uses the first that works. `AccessDeniedException` ("not available for this account") and `ResourceNotFoundException` (Anthropic's first-use form) mean "skip to the next version"; a daily-token throttle (an allowance of zero or used up) does the same, because each Sonnet version has its own; ordinary per-minute throttling is raised and the person is told the bot is busy. The working model is remembered, and at each 30-minute refresh both it and the skipped list are forgotten, so granted access is noticed and a lower fallback never sticks.

**Prompt-injection controls.** Merchant text is stripped of control and zero-width characters and of the characters that build links, code spans and spoilers (`[ ] < > ` |`), and cut to 80 characters when stored. The model only sees it inside quoted JSON, and the system prompt says it is data. Replies ask Discord not to unfurl links. A category change is refused unless all of these hold:

- the person's own question **opens with a request** ("move ...", "please undo ...", "can you change ..."), with no negation, and a question mark, a "why/what/how" aside, or a number that is only a count of days does not enable a write;
- for a move, the question **names the target category**, so injected text cannot choose a different one;
- the transaction came from a search in the same conversation, and no other change was made in that turn;
- the same Discord interaction has not already made a change. A redelivered request replays the stored result instead of, for example, undoing a second correction.

A **budget change** has its own, stricter gate. The question must open with a request that is not advice-seeking ("raise ...", "can we move ...", not "should we ...?"), contain a number and no negation, and the tool then requires that every category is named, every amount is one the person wrote (dates and "10 K-cups" are not amounts), the month is the one they named (this month unless they said next month), and the direction matches (raise or lower, *to* or *by*, and in "from A to B" A goes down and B up). Only this month and next month can change. All arithmetic is exact, in code, and all changes in a request apply together or not at all.

Three more kinds of change follow the same pattern, each with its own gate and its own checks of the model's arguments against the person's words:

- **Charge adjustment.** The question must be a statement or request (not a question, doubt, hypothetical or denial) with an amount, and say what the charge counts for or that money came back. The model gives either `counted_amount` or `reimbursed_amount`, which must be one the person wrote and must match how they said it (money paid back versus what it really counts for); a charge paid back in full needs words that say so. The charge must be one the model found by searching with a word from the person's own sentence, or one whose card amount they stated and no other found charge shares. Pending charges, refunds, payments and uncounted records are refused.
- **Merchant rule.** The question must be a verb-led instruction with a standing word ("always", "from now on", "every time"), and the rule text and category must appear in it. The rule text must be specific (see above) and matches whole words, ignoring apostrophes. Precedence is the person's correction of one charge, then the household rule, then the built-in Costco/Target rule, then the provider's category. The effect reported is computed by re-resolving this month's charges under the new rules.
- **Standing limit.** `from_now_on` on a budget change adds a dated step. Steps are never dropped (at most 60), undo removes the last one, and a one-off limit for a month still wins over a standing step.

Undo requests are matched by the words right after the verb ("undo the rule, the budget was fine" is about the rule). Because each write is audited under its own subject (`adjust:<id>`, `rule:<text>`, `default:<month>`), one kind of undo never reads another's history.

After any change, the reply is built by the program from what was saved, not by the model, and the model is not asked again. A refused change is always reported as not done, and a redelivered Discord request is answered from its stored result before the model or the daily cap is consulted.

Changes are stored beside, never over, the provider's category and carry the Discord user ID in an append-only audit log. A month's budget changes live on one record that also holds the changes that produced them (for undo) and a version number, saved with a conditional write, so two people changing the budget at once cannot lose each other's change. Adjustments, merchant rules and standing limits are each stored on versioned records with the same conditional write, keeping up to 20 changes for undo. The worker's ledger write permission is limited to the correction, audit, replay-mark, budget, adjustment, merchant-rule and standing-limit partitions, and it may delete only an undone correction. **Residual risk:** if two charges match the person's words, a hostile payee name could still influence which one the model picks. The reply names the merchant, amount and date that moved, and one command (`undo that`) reverses it. A button confirmation would close this and is not built.

## Card data

1. **Link.** `scripts/plaid_link.py` creates a Hosted Link session. Plaid shows the bank login; the app never sees credentials. The resulting access token (read-only Transactions) goes straight into the `household-budget/plaid` secret. Only credit accounts are offered, and each is labelled with one of the household's two card keys. Duplicate labels are refused.
2. **Sync.** The sync Lambda runs at 7:30 AM and 7:30 PM Central, and when Plaid's webhook arrives. It takes a per-feed lock, calls `/transactions/sync` from the saved cursor, follows `has_more`, restarts from the first cursor if Plaid reports a mutation mid-pagination, upserts by stable ID, applies removals, then saves the cursor and a freshness record. A malformed record fails the sync loudly rather than silently understating spending. Errors that need the household (`ITEM_LOGIN_REQUIRED`) are recorded and not retried. Temporary ones raise so the schedule or Lambda retries: network and server errors, rate limits, and the codes Plaid reports as ordinary client errors such as `INSTITUTION_DOWN` and `PRODUCT_NOT_READY`.
3. **Webhook.** `POST /plaid` requires Plaid's `Plaid-Verification` JWT: ES256 only, key fetched by `kid` and cached, signature checked, issue time within five minutes, body SHA-256 compared in constant time. Forged tokens cannot burn Plaid API calls: any failed key lookup is remembered for a minute, no more than five lookups are made per minute per warm runtime however many key IDs arrive, and the Plaid timeout is four seconds. A webhook that arrives while a sync is running waits up to 45 seconds for it, then syncs again, instead of failing. A verified webhook for a linked item starts the sync Lambda asynchronously and returns at once.
4. **Ledger.** One DynamoDB table (`pk`, `sk`, and a month index). Transactions are keyed by stable ID and expire 800 days after their spend date; corrections, monthly budget changes, charge adjustments, merchant rules, standing limits, an append-only audit trail, cursors, freshness and locks live beside them and are tiny. A month is one indexed query.
5. **What counts.** Spend date is the day a purchase was authorized (else posted). Pending and posted are tracked separately and remaining budget includes pending. Card payments (credits named like "AUTOMATIC PAYMENT", or negative `LOAN_PAYMENTS`/`TRANSFER_OUT`), paychecks, rewards credits, account transfers and non-USD records are excluded; a positive amount in `LOAN_PAYMENTS` or `TRANSFER_OUT` is a real charge and counts. A store return still counts when the bank labels it income or a transfer, and an Amazon return with no useful category counts as shopping. Refunds reduce the month's spent total, which leaves more of the budget. A pending record disappears when its posted replacement names it, or when another feed already shows the posted version. The same purchase from several feeds (a duplicated or replaced Plaid Item, or a CSV that overlaps Plaid) is counted once, preferring Plaid: records match when the card and amount agree and any of their dates are within a day (four for a pending record); two genuinely identical purchases inside one feed both count. A correction follows its purchase: made on a pending charge, it applies to the posted replacement, and made on a CSV row, it applies to the Plaid record that replaces it. A posted record that replaces a pending one keeps the earlier purchase date, so a charge made on the 31st stays in its month.
   Freshness is judged per card: a card is healthy if any feed covering it is recent and error-free, so an old backfill CSV or a replaced connection does not leave a permanent warning. A CSV counts as current only as far as its newest row.
6. **Costco and Target** move from Shopping to Groceries and household only when the provider chose Shopping. A person's correction beats the rule.

## Daily update

EventBridge Scheduler invokes the report Lambda at `cron(0 8 * * ? *)` in `America/Chicago`, so daylight saving changes don't move it. The Lambda claims `MSG#report-<Central date>`, so a scheduler retry or manual run posts at most once per day. In live mode it reads the webhook URL from Secrets Manager and posts. Scheduler retries failures twice within one hour.

The message is built from the ledger by Python alone: month-to-date total against $6,280 with pending, days left, pace against the share of the month elapsed (exact in hours, so daylight saving does not skew it), shopping, groceries and dining lines, any category over its cap, what posted yesterday (totals only, no merchant names), one rule-based suggestion, and a data-freshness line that names stale, failing or unconnected cards. If nothing has ever synced it says cards are not connected and shows no totals. If the ledger cannot be read it still posts a plain apology. It never involves the model, so a model outage cannot block it.

Completed records have a 90-day TTL, with expiry checked in application conditions rather than relying on prompt DynamoDB TTL deletion. Claims are guarded by ownership tokens so an old worker cannot delete or complete a newer worker's work.

## Infrastructure choices

- One account and region: an API Gateway HTTP API, Python 3.14 arm64 Lambdas (webhook, worker, report always; sync and Plaid webhook when `PLAID_ENABLED`), SQS with a dead-letter queue, two on-demand DynamoDB tables, and EventBridge schedules. There is no VPC, NAT, EC2, or Gateway connection.
- The HTTP API `$default` stage is throttled to 10 requests per second with a burst of 20, which bounds cost from junk traffic. The SQS trigger is capped at two concurrent workers.
- The 90-second worker has a 540-second SQS visibility timeout, six times the function timeout as AWS recommends. [AWS SQS event source configuration](https://docs.aws.amazon.com/lambda/latest/dg/services-sqs-configure.html).
- Queue retention is four days and dead-letter retention fourteen. Batch size is one, partial batch responses are on, and both queues deny non-TLS access.
- Both DynamoDB tables have point-in-time recovery and are retained on deletion. Log groups keep events for 30 days and are retained on delete.
- IAM, by function: only the webhook can send to the queue and only the worker can consume it. The worker and sync write the ledger; the report only reads it. Only the report reads `household-budget/discord`. Only the sync and Plaid webhook read `household-budget/plaid`. Only the Plaid webhook can invoke the sync. Only the worker can call Bedrock, only for Claude Sonnet, and only when `AI_ENABLED`. CDK imports both secrets by name; it does not create them. Each scheduler role trusts only this account's default schedule group.
- Optional with `ALERT_EMAIL`: an SNS topic and email subscription, an alarm for errors in each function, one for a non-empty dead-letter queue, one for the 8 AM report not running in 24 hours, and a $50 monthly cost budget alerting at 80% actual and 100% forecast. These notify; they do not stop spending. The email subscription must be confirmed once from the message AWS sends. The "report did not run" alarm treats a missing report as a failure, so it sits in the ALARM state from creation until the first 8 AM run after that, then clears.
- The stack has termination protection on, so deleting it takes a deliberate extra step. The data tables are also retained on deletion.
- Configuration rejects malformed IDs and small-order Ed25519 public keys. The `.env.example` placeholders fail validation, so they cannot be deployed by mistake.
- Discord calls send a `DiscordBot` User-Agent and disable mentions, so a reply cannot ping anyone.
- Logs contain outcomes, error codes or exception class names only. Questions, user IDs, interaction tokens, the webhook URL, Plaid tokens, merchant names and amounts are not logged.
- CDK feature flags are the `aws-cdk-lib` recommended values in `cdk.json`. Review `cdk diff` before changing them after a deployment.

## Remaining limits before production

- **Delivery is not exactly once.** A daily post that Discord accepts just before a timeout or crash can be retried and posted twice. Retried reply edits change the same message, which is harmless.
- **Interaction tokens expire after 15 minutes.** A question stuck in retries longer than that ends in the dead-letter queue unanswered.
- Webhook cold start must stay under Discord's 3-second acknowledgement limit. The webhook only verifies and queues, but live latency has not been measured.
- **Plaid and AI are off until you enable them.** The worker then calls the highest Sonnet the account can use. See [decision 003](decisions/003-sonnet-on-bedrock-and-plaid.md#open-items).
- Re-linking an expired bank login is remove then add (answer `y` to delete the old connection's stored transactions, otherwise the new connection's copy of the same purchases is matched against them); Plaid's update mode is not implemented.
- A transaction whose provider category is wrong needs a correction by a person; the mapping is a reporting convention, not proof of what was bought.
- The morning post is a point-in-time summary of data Plaid last refreshed, which is one to four times a day. "Data through ..." is the time of our last successful sync, not necessarily the bank's last refresh.
- **Bedrock may still be refused after the eligibility problem is fixed.** The `us.` profile can route to us-east-1 or us-west-2, and this project's managed policies may deny Regions other than us-east-2. Run `scripts/check_ai_access.py` after submitting Anthropic's form; an SCP denial would look different from "not available for this account".
- **The public Plaid webhook Lambda can read the whole Plaid secret**, including bank access tokens, because the keys and the linked-item list share one secret. Its code path is small and verified, but splitting the credentials from the tokens would remove the exposure at the cost of a second secret.
- **The scoped CloudFormation role can create IAM roles named `HouseholdBudget*` and write inline policies on them, without a permissions boundary.** Attaching managed policies is limited to Lambda's logging policy, which closes the easiest escalation, but an inline policy could still grant more. That only matters if a template you did not write is ever deployed through it; deploys here are local, from this repository. Add a boundary before enabling GitHub deploys.
- Amounts spelled out in words, in an AI answer, are not caught by the number check.
