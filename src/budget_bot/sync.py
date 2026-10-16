"""Pulls new transactions into the ledger and records how fresh each feed is.

Runs twice a day on a schedule and whenever Plaid's webhook says updates are waiting. Both
paths are safe to repeat: the ledger is upserted by stable transaction ID and the cursor only
advances after the batch is stored.
"""

import logging
import os
import time
from collections.abc import Callable
from dataclasses import dataclass, replace
from datetime import UTC, datetime
from typing import Any
from uuid import uuid4

from budget_bot.config import Settings
from budget_bot.credentials import plaid_config
from budget_bot.ledger import DynamoLedger, ledger_table
from budget_bot.plaid import PlaidClient, PlaidProvider
from budget_bot.ports import FeedStatus, Ledger, ProviderError, SyncState, TransactionProvider
from budget_bot.transactions import Transaction

logger = logging.getLogger(__name__)
logger.setLevel(logging.INFO)

# Longer than one feed's sync can take, so a crashed run unlocks itself.
LOCK_SECONDS = 120
LOCK_POLL_SECONDS = 3.0
# A webhook-triggered run waits this long for a sync already in progress (Lambda allows 90).
WEBHOOK_WAIT_SECONDS = 45.0


@dataclass(frozen=True)
class SyncReport:
    outcomes: dict[str, str]  # feed -> "ok", "busy" or an error code
    retry: bool  # at least one failure is worth retrying


def _keep_purchase_date(txn: Transaction, ledger: Ledger) -> Transaction:
    """A posted record that replaces a pending one keeps the earlier purchase date.

    Plaid usually sends an authorization date on both, but when it does not, the posted record
    would carry the posting date and a purchase on the 31st could slip into the next month.
    """
    if txn.pending or txn.pending_of is None:
        return txn
    pending = ledger.get(txn.pending_of)
    if pending is not None and pending.spend_date < txn.spend_date:
        return replace(txn, spend_date=pending.spend_date)
    return txn


def _acquire(
    state: SyncState, name: str, owner: str, wait_seconds: float, sleep: Callable[[float], None]
) -> bool:
    """Take the feed's lock, waiting up to `wait_seconds` for a run already in progress.

    A webhook that arrives mid-sync waits and then syncs again, because the running sync may have
    read Plaid just before the new data landed. Waiting avoids failing the Lambda, which would
    raise a false alarm.
    """
    waited = 0.0
    while not state.lock(name, owner, LOCK_SECONDS):
        if waited >= wait_seconds:
            return False
        sleep(LOCK_POLL_SECONDS)
        waited += LOCK_POLL_SECONDS
    return True


def run_sync(
    provider: TransactionProvider,
    ledger: Ledger,
    state: SyncState,
    now: datetime,
    only: str | None = None,
    wait_seconds: float = 0.0,
    sleep: Callable[[float], None] | None = None,
) -> SyncReport:
    stamp = now.astimezone(UTC).isoformat()
    previous = {status.feed: status for status in state.statuses()}
    outcomes: dict[str, str] = {}
    retry = False
    for feed in provider.feeds():
        if only is not None and feed != only:
            continue
        owner = uuid4().hex
        if not _acquire(state, f"sync-{feed}", owner, wait_seconds, sleep or time.sleep):
            outcomes[feed] = "busy"
            continue
        try:
            batch = provider.fetch(feed, state.cursor(feed))
            ledger.upsert([_keep_purchase_date(txn, ledger) for txn in batch.upserts])
            ledger.remove(batch.removed)
            if batch.cursor:
                state.save_cursor(feed, batch.cursor)
            state.save_status(FeedStatus(feed, stamp, None, batch.accounts))
            outcomes[feed] = "ok"
        except Exception as exc:
            code = exc.code if isinstance(exc, ProviderError) else "SYNC_FAILED"
            earlier = previous.get(feed)
            # Keep the last success time so the report can say how old the data is.
            state.save_status(
                FeedStatus(
                    feed,
                    earlier.last_success if earlier else None,
                    code,
                    earlier.accounts if earlier else (),
                )
            )
            outcomes[feed] = code
            retry = retry or not isinstance(exc, ProviderError) or exc.retryable
        finally:
            state.unlock(f"sync-{feed}", owner)
    return SyncReport(outcomes, retry)


def lambda_handler(event: dict[str, Any], context: Any) -> dict[str, Any]:
    settings = Settings.from_env()
    if not settings.plaid_enabled:
        return {"outcome": "disabled"}
    # Always the latest secret: a card linked a moment ago must be picked up by the next run.
    config = plaid_config(settings.plaid_secret_name, fresh=True)
    provider = PlaidProvider(
        PlaidClient(config.environment, config.client_id, config.secret), config
    )
    only = event.get("feed") if isinstance(event, dict) else None
    if only is not None and only not in provider.feeds():
        # A webhook for an item we no longer track, for example one that was unlinked.
        return {"outcome": "ignored"}
    ledger = DynamoLedger(ledger_table(os.environ["LEDGER_TABLE"]))
    wait = WEBHOOK_WAIT_SECONDS if only is not None else 0.0
    report = run_sync(provider, ledger, ledger, datetime.now(UTC), only, wait)
    ok = sum(1 for outcome in report.outcomes.values() if outcome == "ok")
    logger.info("sync_finished ok=%d of=%d", ok, len(report.outcomes))
    for outcome in set(report.outcomes.values()) - {"ok", "busy"}:
        logger.warning("sync_failed error_code=%s", outcome)
    if report.retry:
        # Raising lets Scheduler or Lambda's async retry try again; errors needing the
        # household (a re-login) are recorded for the report and are not retried.
        raise RuntimeError("sync failed with a retryable error")
    return {"outcome": "ok" if ok == len(report.outcomes) else "partial", "feeds": ok}
