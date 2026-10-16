# Use one private Discord channel instead of group MMS

[Back to README](../../README.md) · [Discord setup](../discord-setup.md) · [Roadmap](../roadmap.md)

**Status:** Accepted  
**Date:** 13 October 2026  
**Supersedes:** the native iPhone group MMS requirement and the Telnyx provider

## Context

The original requirement was one native iPhone Messages group with both spouses and a bot number. A bot cannot join an iMessage group, so that meant carrier group MMS through Telnyx. Telnyx blocked the next step: a private household bot would need 10DLC or another carrier registration that was not approved. In the best case, that route was roughly $4–$13 a month before taxes and still unproven on real phones.

Discord gives both spouses one shared thread on their phones for $0. Its HTTP interactions endpoint fits the existing serverless stack: API Gateway, a webhook Lambda, SQS, a worker Lambda, and DynamoDB. Nothing has to run while the bot is idle.

## Decision

Use one private Discord server that only the two spouses belong to, with one budget channel.

- **Questions:** either spouse types `/ask` with a question in that channel. The reply appears in the same channel, so both see it.
- **Daily update:** EventBridge Scheduler runs a Lambda at 8:00 AM America/Chicago every day. It posts one message through that channel's webhook.
- **Receiving:** HTTP interactions, not a Gateway bot. The webhook Lambda checks Discord's Ed25519 signature and a five-minute timestamp window. It also checks the exact application, server, channel, and two user IDs. Anyone else gets a private refusal and nothing is queued.
- **Replying:** the webhook defers within Discord's 3-second limit and queues the question. The worker edits that deferred reply using the interaction token, which lasts 15 minutes. The worker needs no AWS secret.
- **Credentials:** the channel webhook URL is the only Discord secret in AWS. It lives in Secrets Manager as `household-budget/discord`, and only the report Lambda can read it. The bot token is used once from this PC to register `/ask` and is not stored anywhere.
- **Dry run:** `LIVE_MESSAGING_ENABLED=false` stays the default. In dry run, `/ask` gets a private notice that replies are off, and the 8 AM job posts nothing.
- **Weak keys:** configuration rejects small-order Ed25519 public keys. An all-zero placeholder key accepted forged signatures in a local test.

Plain chat messages without `/ask` would need an always-on Gateway connection, meaning an EC2 instance or container. That is out of scope.

## Consequences

- Telnyx, phone numbers, 10DLC, STOP handling, and per-message charges are gone. Discord costs nothing.
- Both spouses install Discord and keep notifications on for the budget channel.
- Discord messages are not end-to-end encrypted, and Discord stores them. The server must stay private, the bot must not be public, and both spouses must consent to spending summaries there.
- The deployed `POST /telnyx` endpoint is replaced by `POST /discord` on the next deploy.
- AWS cost stays small. Secrets Manager is about $0.40 a month once the webhook secret exists. API Gateway, Lambda, SQS, DynamoDB, and Scheduler usage at this volume is cents or less.
- A reply accepted by Discord just before a worker timeout can still be retried and edited again. Editing the same message is harmless. A daily post that succeeds just before a crash could post twice.
