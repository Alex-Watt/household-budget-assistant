# Delivery roadmap

[Back to README](../README.md)

## Intended product

Both spouses ask about shared card spending in one private Discord channel, and both see every answer. A daily update posts there around 8 AM America/Chicago. All money calculations come from decimal-safe Python; Claude Sonnet interprets questions, requests permitted data and calculations through restricted tools, and explains the results.

**Data path:** separately authorized Chase and Robinhood data (Plaid or CSV) → normalized transactions → deterministic budget service → restricted Sonnet tools and explanations → Discord channel. The scheduled report works without the model. ChatGPT Finances credentials and data are unavailable to this application.

| Milestone | Scope | Status / acceptance gate |
|---|---|---|
| 1. Discord messaging | CDK, signature checks, exact server/channel/member allowlist, SQS/DLQ, worker/state, 8 AM schedule, dry run, tests, CI | **Implemented.** Deploy stays off until you enable it in your own account |
| 2. Budget engine | Sample Chase/Robinhood transactions, caps totaling $6,280, decimal-safe calculations, totals in the 8 AM update | **Implemented and tested** against synthetic data |
| 3. Banking | Example cards, read-only connections, sync/import, freshness | **Implemented, off by default.** Sandbox rehearsal, update mode, and a Robinhood CSV preset are still open |
| 4. Claude and operations | Restricted tools, explanations, end-to-end tests, alarms, cost controls | **Implemented, off by default.** The worker calls the highest Sonnet the account can use ([decision 003](decisions/003-sonnet-on-bedrock-and-plaid.md#open-items)) |

## Next concrete steps

1. Deploy from your own machine with messaging, Plaid, and AI off, then turn on only the pieces you want.
2. **Rehearse Plaid in its sandbox** before connecting a real card, then purge any fake data. Step 1.5 of the setup guide.
3. Re-check `scripts/check_ai_access.py` before assuming the newest Sonnet is the one the account can call.
4. Link cards and turn on AI only after the Discord channel passes its go/no-go.
5. **Build Plaid update mode** for re-linking an expired login. It re-authenticates the same connection, so it uses no new Trial slot (removing a connection does not free one, and the Trial allows 10) and creates no duplicate transactions.
6. Add a Robinhood CSV preset once the export's header row is known.

The [deploy decision](decisions/001-local-aws-deploy.md) still applies: deploy from your own machine, and leave GitHub Actions deployment off unless you explicitly enable it. Plaid and the model stay off in code until that deploy turns them on.
