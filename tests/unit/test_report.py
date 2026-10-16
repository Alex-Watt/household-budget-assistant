from datetime import UTC, date, datetime, timedelta

import pytest

from budget_bot.discord import MockDiscord
from budget_bot.fixtures import fixture_transactions
from budget_bot.ledger_memory import MemoryLedger
from budget_bot.ports import FeedStatus
from budget_bot.report import (
    READ_FAILED,
    build_report,
    handle,
    report_date,
    unconnected_report,
)
from tests.helpers import CHASE, MORNING, MemoryState, budget_service, settings, txn

GROCERY = {"primary": "FOOD_AND_DRINK", "detailed": "FOOD_AND_DRINK_GROCERIES"}
DINING = {"primary": "FOOD_AND_DRINK", "detailed": "FOOD_AND_DRINK_RESTAURANT"}
PETS = {"primary": "GENERAL_MERCHANDISE", "detailed": "GENERAL_MERCHANDISE_PET_SUPPLIES"}


@pytest.mark.parametrize(
    ("now", "expected"),
    [
        # 11:30 PM Central on the 13th is already the 14th in UTC.
        (datetime(2026, 10, 14, 4, 30, tzinfo=UTC), date(2026, 10, 13)),
        # 8 AM on the day daylight saving time starts and the day it ends.
        (datetime(2026, 3, 8, 13, 0, tzinfo=UTC), date(2026, 3, 8)),
        (datetime(2026, 11, 1, 14, 0, tzinfo=UTC), date(2026, 11, 1)),
    ],
)
def test_report_date_uses_central_time(now: datetime, expected: date) -> None:
    assert report_date(now) == expected


def test_unconnected_report_has_no_invented_totals() -> None:
    text = unconnected_report(date(2026, 10, 14))
    assert text.startswith("**Wed Oct 14**")
    assert "no spending totals" in text
    assert "Budget $6,280 · 18 days left" in text
    assert text.count("$") == 1


def test_month_boundaries() -> None:
    assert "1 day left" in unconnected_report(date(2026, 2, 28))
    assert "31 days left" in unconnected_report(date(2026, 12, 1))


def test_report_without_any_synced_data_says_so_instead_of_showing_zero() -> None:
    service = budget_service([], synced=False)
    assert build_report(service) == unconnected_report(date(2026, 10, 14))


def test_report_with_real_totals() -> None:
    service = budget_service(
        [
            txn("a", "400.00", date(2026, 10, 3), "Aldi", **GROCERY),
            txn("b", "100.00", date(2026, 10, 10), "Bistro", **DINING),
            txn("c", "300.00", date(2026, 10, 9), "Petco", **PETS),  # over the $255 pets cap
            txn("d", "50.00", date(2026, 10, 12), "Aldi", posted=date(2026, 10, 13), **GROCERY),
            txn("e", "9.00", date(2026, 10, 13), "Bistro", pending=True, **DINING),
        ]
    )
    text = build_report(service)
    assert text.startswith("**Wed Oct 14**")
    assert "**$859.00** of $6,280.00 (14%)" in text
    assert "**$5,421.00** left · 18 days left" in text
    assert "Pending $9.00" in text
    assert "**Under pace** · 43% of the month gone · about $" in text
    assert "Shopping · $0.00 / $1,500.00 · 0%" in text
    assert "Groceries · $450.00 / $1,300.00 · 35%" in text
    assert "Dining · $109.00 / $750.00 · 15%" in text
    assert "Pets · $300.00 / $255.00 · **$45.00 over**" in text
    assert "Yesterday: $50.00 Groceries" in text
    assert "Hold Pets the rest of the month." in text
    assert "nothing new" not in text and "Repeats" not in text
    assert (
        "Data through Oct 14, 8:00 AM CT for Chase Sapphire Preferred, Robinhood Gold Card." in text
    )
    assert len(text) < 1800


def test_report_uses_no_merchant_names() -> None:
    text = build_report(budget_service([txn("a", "40.00", merchant="Secret Boutique")]))
    assert "Secret Boutique" not in text


def test_report_flags_stale_and_missing_cards() -> None:
    ledger = MemoryLedger()
    old = (MORNING - timedelta(days=3)).isoformat()
    ledger.save_status(FeedStatus("plaid:item1", old, None, (CHASE,)))
    text = build_report(budget_service([txn("a", "5.00")], ledger=ledger, synced=False))
    assert "Heads up: Chase Sapphire Preferred has not refreshed since Oct 11." in text
    assert "Heads up: Robinhood Gold Card is not connected." in text


def test_report_works_on_fixture_data_and_stays_within_discord_limits() -> None:
    ledger = MemoryLedger()
    ledger.upsert(fixture_transactions(date(2026, 10, 14)))
    service = budget_service(ledger=ledger)
    text = build_report(service)
    assert "of $6,280.00" in text and len(text) < 1800
    # Payments to the cards are never counted as spending.
    assert "Payment Thank You" not in text
    # September has both cards' payments, which are left out of its totals.
    assert service.month_summary(2026, 9).excluded_count >= 2


NOW = datetime(2026, 10, 14, 13, 0, tzinfo=UTC)


def test_report_posts_once_per_day() -> None:
    state, discord = MemoryState(), MockDiscord()
    service = budget_service([txn("a", "10.00")])
    assert handle(NOW, state, discord, settings(live=True), service) == "sent"
    assert handle(NOW, state, discord, settings(live=True), service) == "duplicate"
    assert len(discord.posts) == 1 and "**$10.00** of $6,280.00" in discord.posts[0]
    assert state.completed == {"report-2026-10-14"}


def test_dry_run_report_posts_nothing() -> None:
    discord = MockDiscord()
    assert handle(NOW, MemoryState(), discord, settings(), budget_service([])) == "dry_run"
    assert not discord.posts


def test_report_without_a_service_says_cards_are_not_connected() -> None:
    discord = MockDiscord()
    handle(NOW, MemoryState(), discord, settings(live=True))
    assert discord.posts == [unconnected_report(date(2026, 10, 14))]


def test_unreadable_data_still_posts_a_plain_message(caplog: pytest.LogCaptureFixture) -> None:
    class BrokenLedger(MemoryLedger):
        def between(self, start: date, end: date) -> list:
            raise RuntimeError("secret table details")

    discord = MockDiscord()
    service = budget_service([], ledger=BrokenLedger())
    handle(NOW, MemoryState(), discord, settings(live=True), service)
    assert discord.posts[0].endswith(READ_FAILED)
    assert "secret table details" not in caplog.text


def test_failed_post_can_retry() -> None:
    class FailingDiscord(MockDiscord):
        def post(self, text: str) -> None:
            raise RuntimeError("Discord unavailable")

    state = MemoryState()
    with pytest.raises(RuntimeError):
        handle(NOW, state, FailingDiscord(), settings(live=True))
    assert not state.claims and not state.completed
    assert handle(NOW, state, MockDiscord(), settings(live=True)) == "sent"
