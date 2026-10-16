# Banking and AI setup

[Back to README](../README.md) · [Decision 003](decisions/003-sonnet-on-bedrock-and-plaid.md) · [Discord setup](discord-setup.md)

Both features are built and tested, and **off by default** in a fresh checkout. The deployed stack has them on as of 14 October: Plaid sync finished both feeds that morning, and questions were answered by Sonnet 4.6. This guide is how those switches were turned on, and how to rehearse in Plaid's sandbox before changing the live connection.

**Before real cards:** the Discord channel must pass its [go/no-go](discord-setup.md#8-go-live), and both spouses must agree that spending summaries appear there. AWS credentials come from `aws login` on this PC.

## 0. See it first with fake data

No AWS, no accounts, no model:

```powershell
.\.venv\Scripts\python.exe scripts/demo.py
.\.venv\Scripts\python.exe scripts/demo.py --today 2026-10-14 --ask dining --ask summary
```

This runs synthetic Chase and Robinhood data through the real sync, budget engine and 8 AM report code and prints the message the bot would post.

## 1. Connect cards with Plaid

### 1.1 Plaid account

1. Sign up at [dashboard.plaid.com/signup](https://dashboard.plaid.com/signup). New US teams get a free **Trial plan**: real data, up to 10 connected accounts, Transactions included, Chase available immediately. Check the [current terms](https://support.plaid.com/hc/en-us/articles/16110110883479-How-are-Sandbox-Production-Trial-plan-and-Limited-Production-different) when you sign up.
2. In the dashboard, open **Developers → Keys** and keep the **client_id** and the **Sandbox** secret handy. Do not paste them into chat, Git or a file.

### 1.2 Allow the new AWS resources

The scoped CloudFormation role was extended for the new resources. Publish it before deploying (IAM keeps five versions; delete the oldest non-default one if this fails):

```powershell
aws iam create-policy-version --policy-arn arn:aws:iam::123456789012:policy/HouseholdBudgetCfnExecution --policy-document file://infrastructure/cfn-execution-policy.json --set-as-default
```

### 1.3 Store the Plaid keys

The prompt hides the secret and sends it straight to Secrets Manager (`household-budget/plaid`, about $0.40 a month). Start in the sandbox:

```powershell
.\.venv\Scripts\python.exe scripts/plaid_link.py init
```

If you would rather paste the two values into a scratch file (two lines, `key: ...` and `secret: ...`), load it and delete it in one step. The file is checked against Plaid's sandbox first; a secret the sandbox rejects needs `--environment production` (the script never contacts production itself). Git ignores `*secret*.txt` and `keysecret*`, but still keep the file out of the repo folder if you can:

```powershell
.\.venv\Scripts\python.exe scripts/plaid_link.py init --from-file keysecret.txt --delete-file
```

The sandbox and production secrets are different values. Only one is stored at a time; switching is allowed while no cards are linked.

### 1.4 Deploy with Plaid on

The [deploy helper](discord-setup.md#5-deploy-from-this-pc) keeps every live setting and changes only what you name:

```powershell
python scripts/deploy.py --plaid on
```

Note the outputs `PlaidWebhookUrl`, `SyncFunctionName` and `LedgerTableName`, then `$env:LEDGER_TABLE = '<LedgerTableName>'` for the scripts below. Alarm emails and the cost budget are already on if you set `--alert-email`; confirm the subscription email AWS sends.

### 1.5 Rehearse in the sandbox (no real accounts)

```powershell
.\.venv\Scripts\python.exe scripts/plaid_link.py add --sandbox     # label Plaid's fake card when asked
aws lambda invoke --region us-east-2 --function-name <SyncFunctionName> out.json
.\.venv\Scripts\python.exe scripts/ledger_admin.py report          # what the 8 AM message would say
```

The report is read locally; nothing posts to Discord. Check that it shows spending, a "Data through ..." line, and no payments counted. If your channel is live, the next 8 AM post and `/ask summary` would also show this fake data, so clean up before the morning:

```powershell
# Disconnects the fake bank and (answer y) deletes its stored transactions in one step
.\.venv\Scripts\python.exe scripts/plaid_link.py remove --item-id <item id> --table $env:LEDGER_TABLE
```

Do this before step 1.6: `init` refuses to switch from sandbox to production while an item is linked, because an access token only works in the environment that issued it.

### 1.6 Link the real cards

This is the step that needs your explicit go-ahead.

```powershell
.\.venv\Scripts\python.exe scripts/plaid_link.py init      # production secret; choose "production"
.\.venv\Scripts\python.exe scripts/plaid_link.py add --webhook-url <PlaidWebhookUrl>
```

Open the printed link, choose the bank and sign in on Plaid's page. Choose **Chase** for the Sapphire Preferred. For the Robinhood Gold Card, search Plaid's list by the **phone number on your Robinhood account**, as Robinhood's own budgeting guidance says. The script then asks which household card each credit account is. Checking and other accounts on the same login are ignored. Repeat `add` for the second card.

Then sync and check against the card apps:

```powershell
aws lambda invoke --region us-east-2 --function-name <SyncFunctionName> out.json
.\.venv\Scripts\python.exe scripts/ledger_admin.py report
```

Compare against the card apps: month-to-date total, a few pending items, that card payments are not counted, that a refund reduced its category, and that Costco and Target land in Groceries and household. Two cards should both appear on the "Data through" line.

After that the bot syncs itself: Plaid's webhook starts a sync when new data is ready, and a schedule runs at 7:30 AM and 7:30 PM Central, so the 8 AM report is fresh.

### 1.7 When something needs attention

The 8 AM report and `/ask coverage` say when a card is stale, missing, or needs re-linking.

| What you see | Meaning and fix |
|---|---|
| "needs to be re-linked" (`ITEM_LOGIN_REQUIRED`) | The bank login expired. Run `plaid_link.py remove --item-id <id> --table <LedgerTableName>`, answer `y` to delete that connection's stored transactions, then `add` again. A new connection gets new transaction IDs, so its history is fetched again; deleting the old copy first avoids counting the same purchases twice. On the Plaid Trial this also uses up one of your 10 connection slots, because removal does not free it. Without `--table`, run `ledger_admin.py purge --feed plaid:<id>` yourself. |
| "has not refreshed since ..." | No successful sync for 36 hours. Check the Sync Lambda's CloudWatch logs; invoke it by hand. |
| "is not connected" | Only one of the two cards is linked. Run `add` for the other. |
| A second Chase account is missing | Chase does not share new accounts automatically. Remove and add the item again, selecting the account. |

To stop all access, `plaid_link.py remove --item-id <id>` ends Plaid's connection and deletes the token. You can also review connections at [my.plaid.com](https://my.plaid.com).

### 1.8 A card Plaid cannot read: CSV

Export from the card's app or site, then preview and import (nothing is written without `--apply`):

```powershell
.\.venv\Scripts\python.exe scripts/import_csv.py chase.csv --account chase-sapphire-preferred --format chase
.\.venv\Scripts\python.exe scripts/import_csv.py chase.csv --account chase-sapphire-preferred --format chase --apply
```

Re-importing the same file or an overlapping export is safe. `--account` must be one of the two household cards. The `chase` format matches Chase's credit card export. For anything else use `--format generic --spending negative` (or `positive`), which expects `Date`, `Amount` and `Description` columns; say which sign your file uses for purchases, because `--apply` refuses a file whose net spending comes out negative. The Robinhood export's columns are not known yet; send the header row (no data) and a preset can be added. A CSV is treated as current only up to its newest row, so a card fed only by files will show a "has not refreshed" note after ten days. Do not import a card both ways for the same period; if you do, matching purchases are counted once (same card and amount, dates within a day), and Plaid is the source of truth.

## 2. Ask questions with Claude Sonnet

### 2.1 Check what this AWS project can call

```powershell
.\.venv\Scripts\python.exe scripts/check_ai_access.py
```

It tries Sonnet versions from highest to lowest with a few tokens each and prints the first that works.

A newer Sonnet can still answer `AccessDeniedException` ("not available for this account"). That is an availability limit on the account, not something the code changes, and the bot moves up on its own when access clears. Turning a model on usually takes three things:

1. The one-time **use case form** (Amazon Bedrock → Model catalog → a Claude model → *Submit use case details*).
2. A plan that allows third-party models billed through AWS Marketplace. Free plans often exclude them.
3. A **quota above zero**. Some accounts start at 0 tokens. A question takes two to four model calls, so a low request quota can throttle a few questions in a row. The bot then says it is busy and asks you to try again in a minute.

If the check reports `ThrottlingException` ("Too many tokens per day") for a version, its quota for this account is 0: raise it in Service Quotas (Amazon Bedrock, *Cross-region model inference tokens per minute for ...*). If a version is rejected for any other reason, that is new information: the `us.` profile can route to Regions this project's managed policies may deny, and an explicit deny would read differently. Report the message. Whatever the check reports is what the bot will use. A version that is not available, or is out of its daily tokens, is skipped, and the list is re-checked every 30 minutes, so access granted later is picked up without a redeploy.

### 2.2 Turn it on

```powershell
python scripts/deploy.py --ai on
```

This adds Bedrock permission to the worker only, limited to Sonnet. Try, in `#budget`:

- `/ask What did we spend at Amazon last week?`
- `/ask Why did dining jump this month?`
- `/ask Can we afford $200 on clothes?` (the answer states that it only covers card-budget room)
- `/ask Move that Target charge to gifts`, then `/ask Undo that`
- `/ask Raise dining to $900 this month`, `/ask Move $100 from shopping to groceries this month` or `/ask Set travel to $500 for next month`, then `/ask Undo the budget change`
- `/ask The Ruth's Chris charge was really $100, someone paid us back $100`, then `/ask Undo the adjustment`
- `/ask Always count WGC* as gifts`, then `/ask Undo the last rule change`
- `/ask Set dining to $900 every month from now on`, then `/ask Undo the budget change`

### 2.3 What it will and will not do

- `summary`, a category name, `coverage` and `help` are answered by Python without the model.
- Every figure in an AI answer is checked against the tool results. If one is not found, the plain summary is posted instead and the log records `ai_ungrounded`.
- If the model or Bedrock fails, you get the plain summary and a note. The 8 AM post is unaffected.
- After `AI_DAILY_LIMIT` AI answers in a day (Central time), AI answers pause until tomorrow; commands still work.
- It can change one transaction's category per question, only when asked, and `Undo that` reverses the last correction. Run `scripts/ledger_admin.py audit` to see who changed what.
- It can change a month's category limits (this month or next only), only when asked and only with dollar amounts you wrote. "Raise dining to $900", "lower shopping by $200" and "move $100 from shopping to groceries" all work, and the month's total follows because the budget is the sum of its limits. The month, every amount, every category and the direction (raise or lower, *to* or *by*, *from* and *to*) are checked against your words, and a "should we ...?" question never changes anything. A change is reported in the bot's own words from what was saved, and a refused one says so plainly. Past months keep the limits they had. `Undo the budget change` walks back one change at a time (the last 20 per month). The 8 AM post and `summary` say when limits were changed.
- **A charge can count for less than the card shows.** If a $200 gift was really $100 because someone paid you back $100, say so ("the Ruth's Chris charge was really $100, someone paid us back $100"). The charge then counts for $100 in every total, the pace and the 8 AM post, which says how many charges were adjusted but not the merchant. The card's own amount is never rewritten and the adjustment follows the purchase from pending to posted. You name the charge by its merchant (apostrophes and symbols such as the `*` in `WGC*` do not matter) or by its card amount when only one charge has it. Only posted purchases can be adjusted, never a pending charge, a refund, a payment or a charge that is not in the budget. It can only lower what a charge counts for, down to $0 if you say it was paid back in full. `Undo the adjustment` walks back one at a time.
- **A standing rule applies to every matching charge, past and future.** "Always count WGC* as gifts" or "from now on put Ruth's Chris under restaurants" moves every charge whose merchant text contains those words, this month's and earlier months' too, and says how many charges moved this month. The text must be specific: at least 4 letters or digits, or 3 with a `*` as in `WGC*`; it matches whole words, so `mart` never matches Walmart. Your own correction of a single charge always wins over a rule. At most 30 rules. `Remove the WGC* rule` and `Undo the last rule change` put things back.
- **A limit can change "from now on".** "Set dining to $900 every month from now on" changes the limit from this month (or from next month if you say so) for every month after it. Earlier months keep what they had, and a one-off change to a single month still wins over it; the reply says when that is the case. `Undo the budget change` takes back whichever budget change, one-off or standing, is newest.
- Only one change is made per question. If you ask for two, the first is made and the reply says the other was not.
- Each `/ask` stands alone, so the bot cannot hold a conversation. If a request is missing something, it says what and asks you to send it again in full.
- If the model's reply includes a figure no tool returned, it is asked once to rewrite without it before the plain summary is used.
- It never names merchants unless you ask, and never posts full account details.

To turn AI off, redeploy with `AI_ENABLED = 'false'`; the Bedrock permission is removed.

## 3. Go / no-go for real card data and AI

**Go** when all of these hold:

1. The Discord channel passed its [go/no-go](discord-setup.md#8-go-live) and both spouses consented.
2. A sandbox sync produced a report, and `purge` plus `remove` cleaned up.
3. After linking real cards, the report's total matches the card apps within pending differences you can explain.
4. The next 8 AM post shows real totals once, and the dead-letter queue and logs are clean.
5. For AI: `check_ai_access.py` shows a working Sonnet, three or four test questions answered correctly in the channel, and the daily limit tested once.

**No-go** if totals disagree and the cause is not understood, a card shows stale data without a warning, or any secret appears in chat, logs or Git.

## 4. Costs

- **Plaid:** $0 on the Trial plan for this household. It allows 10 connected accounts (Items) with Transactions included, and you need 2, one per card login. Two catches: **removing a connection does not free its slot**, and a duplicate connection to the same bank counts too, so re-linking by remove-then-add uses one of the 10 each time. A [planned update-mode re-link](roadmap.md#next-concrete-steps) avoids that. API calls against connected accounts are not capped. After the Trial, Plaid does not publish a price list: Transactions is a **monthly subscription per connected account** while its access token exists, and the price is shown when you apply for paid Production. Upgrading is one-way. Sandbox rehearsal is always free and uses no slots.
- **AWS:** DynamoDB, Lambda, SQS, API Gateway and Scheduler remain cents at this volume. Secrets Manager is about $0.40 a month per secret, two in total (Discord and Plaid); the model uses IAM and needs no secret. Bedrock bills per token; see [current pricing](https://aws.amazon.com/bedrock/pricing/). The daily limit bounds it.
- **Alarms and budget** (if `ALERT_EMAIL` is set) email you. They do not stop spending; only the project spend limit does.
