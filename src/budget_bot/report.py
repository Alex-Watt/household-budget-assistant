"""Daily 8 AM channel update and its EventBridge Scheduler entry point.

The report is pure Python over the ledger. The language model is never involved, so a model
outage cannot block it. Spending totals appear only when card data has really been synced.
"""

import calendar
import logging
import os
from datetime import date, datetime, timedelta
from decimal import Decimal
from typing import Any

from budget_bot.budget import HOUSEHOLD_TIME, BudgetService
from budget_bot.categories import TOTAL_BUDGET
from budget_bot.config import Settings
from budget_bot.credentials import report_webhook_url
from budget_bot.discord import DiscordChannelWebhook, MockDiscord
from budget_bot.ledger import DynamoLedger, ledger_table
from budget_bot.money import usd
from budget_bot.ports import ChannelPoster, MessagingState
from budget_bot.render import daily_text, days_left_text, report_heading
from budget_bot.state import DynamoMessagingState, state_table

logger = logging.getLogger(__name__)
logger.setLevel(logging.INFO)

READ_FAILED = (
    "I couldn't read the card data this morning, so there are no spending totals today. "
    "I'll try again tomorrow."
)


def report_date(now: datetime) -> date:
    return now.astimezone(HOUSEHOLD_TIME).date()


def unconnected_report(today: date, budget: Decimal = TOTAL_BUDGET) -> str:
    days_left = calendar.monthrange(today.year, today.month)[1] - today.day + 1
    return (
        f"{report_heading(today)}\n"
        "No card data yet, so no spending totals.\n"
        f"Budget {usd(budget).removesuffix('.00')} · {days_left_text(days_left)}"
    )


def build_report(service: BudgetService) -> str:
    """Compose the morning message from real ledger totals, or say plainly that none exist."""
    coverage = service.coverage()
    today = service.today()
    if not coverage.connected:
        return unconnected_report(today, service.budget_total())
    return daily_text(
        today,
        service.month_summary(),
        service.posted_on(today - timedelta(days=1)),
        coverage,
    )


def handle(
    now: datetime,
    state: MessagingState,
    poster: ChannelPoster,
    settings: Settings,
    service: BudgetService | None = None,
) -> str:
    today = report_date(now)
    key = f"report-{today.isoformat()}"
    lease = state.claim(key)
    if lease is None:
        return "duplicate"
    try:
        if settings.live_messaging:
            poster.post(_compose(today, service))
            result = "sent"
        else:
            result = "dry_run"
        state.complete(key, lease)
        return result
    except Exception:
        state.release(key, lease)
        raise


def _compose(today: date, service: BudgetService | None) -> str:
    if service is None:
        return unconnected_report(today)
    try:
        return build_report(service)
    except Exception as exc:
        # A data problem must not stop the morning post; say so plainly and log only the type.
        logger.warning("report_data_failed error_type=%s", type(exc).__name__)
        return f"{report_heading(today)}\n{READ_FAILED}"


def lambda_handler(event: dict[str, Any], context: Any) -> dict[str, str]:
    settings = Settings.from_env()
    state = DynamoMessagingState(state_table(os.environ["STATE_TABLE"]))
    poster: ChannelPoster = (
        DiscordChannelWebhook(report_webhook_url(settings.secret_name))
        if settings.live_messaging
        else MockDiscord()
    )
    ledger = DynamoLedger(ledger_table(os.environ["LEDGER_TABLE"]))
    service = BudgetService(ledger, ledger, lambda: datetime.now(HOUSEHOLD_TIME))
    outcome = handle(datetime.now(HOUSEHOLD_TIME), state, poster, settings, service)
    logger.info("report_outcome=%s", outcome)
    return {"outcome": outcome}
