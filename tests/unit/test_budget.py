from datetime import UTC, date, datetime, timedelta
from decimal import Decimal

import pytest

from budget_bot.budget import (
    PERIODS,
    BudgetService,
    RecategorizeError,
    month_elapsed,
    resolve_period,
)
from budget_bot.categories import Category
from budget_bot.ledger_memory import MemoryLedger
from budget_bot.ports import FeedStatus
from budget_bot.render import totals_text
from tests.helpers import CHASE, MORNING, ROBINHOOD, budget_service, txn

GROCERY = {"primary": "FOOD_AND_DRINK", "detailed": "FOOD_AND_DRINK_GROCERIES"}
SHOPPING = {"primary": "GENERAL_MERCHANDISE", "detailed": "GENERAL_MERCHANDISE_ONLINE_MARKETPLACES"}
SUPERSTORE = {"primary": "GENERAL_MERCHANDISE", "detailed": "GENERAL_MERCHANDISE_SUPERSTORES"}
DINING = {"primary": "FOOD_AND_DRINK", "detailed": "FOOD_AND_DRINK_RESTAURANT"}


def october_ledger() -> list:
    return [
        txn("t1", "100.00", date(2026, 10, 3), "Whole Foods", **GROCERY),
        txn("t2", "200.00", date(2026, 10, 5), "Costco Wholesale", **SUPERSTORE),
        txn("t3", "50.00", date(2026, 10, 6), "Amazon", **SHOPPING),
        txn("t4", "-20.00", date(2026, 10, 7), "Amazon", **SHOPPING),
        txn("t5", "10.00", date(2026, 10, 13), "Bistro", pending=True, **DINING),
        txn("t6", "-500.00", date(2026, 10, 8), "Payment", primary="LOAN_PAYMENTS"),
        txn("t7", "70.00", date(2026, 9, 30), "Whole Foods", **GROCERY),
        txn("t8", "80.00", date(2026, 11, 1), "Whole Foods", **GROCERY),
    ]


def test_month_summary_separates_posted_pending_refunds_and_exclusions() -> None:
    summary = budget_service(october_ledger()).month_summary()
    assert summary.line(Category.GROCERIES).posted == Decimal("300.00")  # Costco rule applied
    assert summary.line(Category.SHOPPING).posted == Decimal("30.00")  # refund nets out
    assert summary.line(Category.DINING).pending == Decimal("10.00")
    assert (summary.posted, summary.pending, summary.committed) == (
        Decimal("330.00"),
        Decimal("10.00"),
        Decimal("340.00"),
    )
    assert summary.budget == Decimal("6280")
    assert summary.remaining == Decimal("5940.00")
    assert summary.excluded_count == 1  # the card payment
    assert summary.refunds == Decimal("20.00")
    # September and November purchases are outside October.
    assert summary.line(Category.GROCERIES).committed == Decimal("300.00")


def test_returns_increase_what_is_left_of_the_monthly_budget() -> None:
    rows = [
        txn("buy", "80.00", date(2026, 10, 4), "Amazon", **SHOPPING),
        txn(
            "back",
            "-30.00",
            date(2026, 10, 9),
            "AMZN Mktp",
            primary="TRANSFER_IN",
            detailed="TRANSFER_IN_ACCOUNT_TRANSFER",
        ),
        txn("other", "-12.00", date(2026, 10, 11), "Nordstrom Rack", primary="INCOME"),
        txn("pay", "-500.00", date(2026, 10, 8), "Payment Thank You", primary="LOAN_PAYMENTS"),
        txn(
            "rewards", "-15.00", date(2026, 10, 10), "ULTIMATE REWARDS CASH BACK", primary="INCOME"
        ),
    ]
    summary = budget_service(rows).month_summary()
    assert summary.posted == Decimal("38.00")  # 80 - 30 Amazon - 12 Nordstrom
    assert summary.refunds == Decimal("42.00")
    assert summary.remaining == Decimal("6280") - Decimal("38.00")
    assert summary.line(Category.SHOPPING).posted == Decimal("50.00")
    assert summary.line(Category.MISC).posted == Decimal("-12.00")
    assert summary.excluded_count == 2  # the card payment and the rewards credit
    assert "Refunds of $42.00 are included in that spent total." in totals_text(summary)
    assert "$6,242.00 left" in totals_text(summary)


def test_calendar_dates_decide_the_month_not_utc() -> None:
    late = [
        txn("a", "1.00", date(2026, 10, 31)),
        txn("b", "2.00", date(2026, 11, 1)),
    ]
    assert budget_service(late).month_summary(2026, 10).committed == Decimal("1.00")
    assert budget_service(late).month_summary(2026, 11).committed == Decimal("2.00")


def test_elapsed_fraction_is_exact_in_central_time() -> None:
    # 14 Oct 08:00 CDT is 13 days and 8 hours into a 744-hour October.
    assert month_elapsed(MORNING, 2026, 10) == (Decimal(320) / Decimal(744)).quantize(
        Decimal("0.000001")
    )
    assert month_elapsed(MORNING, 2026, 9) == 1
    assert month_elapsed(MORNING, 2026, 11) == 0


def test_elapsed_fraction_respects_daylight_saving_changes() -> None:
    # November has 721 hours because clocks fall back on the 1st; 15 Nov 12:00 CST is 349 in.
    noon_cst = datetime(2026, 11, 15, 18, 0, tzinfo=UTC)
    assert month_elapsed(noon_cst, 2026, 11) == (Decimal(349) / Decimal(721)).quantize(
        Decimal("0.000001")
    )
    # March has 743 hours because clocks spring forward on the 8th.
    midnight_march_8 = datetime(2026, 3, 8, 6, 0, tzinfo=UTC)  # 00:00 CST, before the change
    assert month_elapsed(midnight_march_8, 2026, 3) == (Decimal(7 * 24) / Decimal(743)).quantize(
        Decimal("0.000001")
    )


@pytest.mark.parametrize(
    ("now", "today", "days_left"),
    [
        # 11:30 PM CDT on the 31st is already November 1st in UTC.
        (datetime(2026, 11, 1, 4, 30, tzinfo=UTC), date(2026, 10, 31), 1),
        (datetime(2026, 11, 1, 5, 0, tzinfo=UTC), date(2026, 11, 1), 30),
        (datetime(2026, 10, 14, 13, 0, tzinfo=UTC), date(2026, 10, 14), 18),
    ],
)
def test_today_and_days_left_follow_the_household_clock(
    now: datetime, today: date, days_left: int
) -> None:
    service = budget_service([], now=now)
    assert service.today() == today
    assert service.month_summary(today.year, today.month).days_left == days_left


def test_pace_labels() -> None:
    def pace(amount: str, now: datetime = MORNING) -> str:
        return (
            budget_service([txn("a", amount, date(2026, 10, 2), **SHOPPING)], now)
            .month_summary()
            .pace
        )

    assert pace("300.00") == "under"  # 5% used vs 43% elapsed
    assert pace("2700.00") == "on"  # 43% used
    assert pace("3500.00") == "over"  # 56% used
    early = datetime(2026, 10, 2, 13, 0, tzinfo=UTC)
    assert pace("100.00", early) == "early"
    assert budget_service([], early).month_summary().projected is None


def test_projection_extends_the_current_pace() -> None:
    summary = budget_service([txn("a", "430.11", date(2026, 10, 2), **SHOPPING)]).month_summary()
    # $430.11 is spent 0.430108 of the way through the month; $430.11 / 0.430108 rounds to $1,000.00.
    assert summary.projected == Decimal("1000.00")


def test_user_correction_moves_money_and_can_be_undone_with_an_audit_trail() -> None:
    service = budget_service(october_ledger())
    before, after = service.recategorize("t3", Category.GIFTS, "100000000000000004")
    assert (before, after) == (Category.SHOPPING, Category.GIFTS)
    summary = service.month_summary()
    assert summary.line(Category.GIFTS).posted == Decimal("50.00")
    assert summary.line(Category.SHOPPING).posted == Decimal("-20.00")
    assert service.undo_recategorization("t3", "100000000000000004") == Category.SHOPPING
    assert service.month_summary().line(Category.GIFTS).posted == 0
    log = service.ledger.audit_log(10)
    assert [(e.action, e.from_category, e.to_category) for e in log] == [
        ("undo", "gifts_donations", "shopping"),
        ("recategorize", "shopping", "gifts_donations"),
    ]
    assert log[0].user_id == "100000000000000004"


def test_recategorize_to_current_category_writes_nothing() -> None:
    service = budget_service(october_ledger())
    assert service.recategorize("t3", Category.SHOPPING, "u") == (
        Category.SHOPPING,
        Category.SHOPPING,
    )
    assert service.ledger.audit_log(10) == []


def test_recategorize_and_undo_reject_unknown_targets() -> None:
    service = budget_service(october_ledger())
    with pytest.raises(RecategorizeError):
        service.recategorize("missing", Category.GIFTS, "u")
    with pytest.raises(RecategorizeError):
        service.undo_recategorization("t3", "u")


def test_correction_applies_to_the_original_not_to_look_alikes() -> None:
    items = [
        txn("a", "40.00", merchant="Target", **SUPERSTORE),
        txn("b", "60.00", merchant="Target", **SUPERSTORE),
    ]
    service = budget_service(items)
    service.recategorize("a", Category.GIFTS, "u")
    summary = service.month_summary()
    assert summary.line(Category.GIFTS).posted == Decimal("40.00")
    assert summary.line(Category.GROCERIES).posted == Decimal("60.00")


def test_search_filters_and_totals_every_match_even_when_rows_are_limited() -> None:
    items = [txn(f"a{i}", "10.00", date(2026, 10, i + 1), "Amazon", **SHOPPING) for i in range(5)]
    items.append(txn("z", "99.00", date(2026, 10, 9), "Whole Foods", **GROCERY))
    service = budget_service(items)
    result = service.search(date(2026, 10, 1), date(2026, 10, 14), merchant="amazon", limit=2)
    assert (result.count, result.total, len(result.rows), result.truncated) == (
        5,
        Decimal("50.00"),
        2,
        True,
    )
    assert [row.spend_date.day for row in result.rows] == [5, 4]  # newest first
    only_groceries = service.search(
        date(2026, 10, 1), date(2026, 10, 14), category=Category.GROCERIES
    )
    assert only_groceries.total == Decimal("99.00")


def test_search_reports_pending_separately_and_skips_payments() -> None:
    service = budget_service(october_ledger())
    result = service.search(date(2026, 10, 1), date(2026, 10, 31))
    assert (result.posted, result.pending) == (Decimal("330.00"), Decimal("10.00"))
    assert all(row.merchant != "Payment" for row in result.rows)


@pytest.mark.parametrize(
    ("start", "end", "merchant"),
    [
        (date(2026, 10, 14), date(2026, 10, 1), None),
        (date(2024, 1, 1), date(2026, 10, 1), None),
        (date(2026, 10, 1), date(2026, 10, 2), "a"),
    ],
)
def test_search_rejects_bad_arguments(start: date, end: date, merchant: str | None) -> None:
    with pytest.raises(ValueError):
        budget_service([]).search(start, end, merchant=merchant)


def test_periods_resolve_deterministically() -> None:
    today = date(2026, 10, 14)  # a Wednesday
    assert resolve_period("today", today) == (today, today)
    assert resolve_period("yesterday", today) == (date(2026, 10, 13),) * 2
    assert resolve_period("last_7_days", today) == (date(2026, 10, 8), today)
    assert resolve_period("this_week", today) == (date(2026, 10, 12), today)
    assert resolve_period("last_week", today) == (date(2026, 10, 5), date(2026, 10, 11))
    assert resolve_period("this_month", today) == (date(2026, 10, 1), today)
    assert resolve_period("last_month", today) == (date(2026, 9, 1), date(2026, 9, 30))
    assert resolve_period("last_30_days", today) == (date(2026, 9, 15), today)
    assert resolve_period("last_month", date(2026, 1, 20)) == (
        date(2025, 12, 1),
        date(2025, 12, 31),
    )
    assert all(resolve_period(name, today) for name in PERIODS)
    with pytest.raises(ValueError):
        resolve_period("next_week", today)


def test_explain_change_compares_matching_days_and_names_the_drivers() -> None:
    items = [
        txn("a", "100.00", date(2026, 10, 3), "Bistro", **DINING),
        txn("b", "40.00", date(2026, 10, 10), "Cafe", **DINING),
        txn("c", "30.00", date(2026, 9, 5), "Bistro", **DINING),
        txn("d", "40.00", date(2026, 9, 20), "Cafe", **DINING),  # after day 14, so excluded
    ]
    result = budget_service(items).explain_change(Category.DINING)
    assert result.through_day == 14
    assert (result.this_period, result.last_period, result.last_full_month) == (
        Decimal("140.00"),
        Decimal("30.00"),
        Decimal("70.00"),
    )
    assert result.delta == Decimal("110.00")
    assert [(c.merchant, c.delta) for c in result.top_increases] == [
        ("Bistro", Decimal("70.00")),
        ("Cafe", Decimal("40.00")),
    ]


def test_check_purchase_reports_category_and_overall_room() -> None:
    service = budget_service(october_ledger())
    check = service.check_purchase(Category.GROCERIES, Decimal("1100"))
    assert check.cap == Decimal("1300")
    assert check.category_after == Decimal("-100.00")
    assert not check.fits_category
    assert check.overall_after == Decimal("4840.00")
    assert check.fits_overall
    with pytest.raises(ValueError):
        service.check_purchase(Category.GROCERIES, Decimal("0"))


def test_posted_on_counts_what_posted_that_day_not_what_was_bought() -> None:
    items = [
        txn("a", "30.00", date(2026, 10, 11), "Cafe", posted=date(2026, 10, 13), **DINING),
        txn("b", "20.00", date(2026, 10, 12), "Aldi", posted=date(2026, 10, 13), **GROCERY),
        txn("c", "9.00", date(2026, 10, 13), "Bistro", pending=True, **DINING),
        txn("d", "5.00", date(2026, 10, 12), "Old", posted=date(2026, 10, 12), **DINING),
    ]
    summary = budget_service(items).posted_on(date(2026, 10, 13))
    assert (summary.count, summary.total) == (2, Decimal("50.00"))
    assert summary.largest is not None and summary.largest.amount == Decimal("30.00")


def sync_status(service: BudgetService, **overrides: object) -> BudgetService:
    ledger = service.ledger
    assert isinstance(ledger, MemoryLedger)
    base = {"feed": "plaid:item1", "last_success": MORNING.isoformat(), "last_error": None}
    base.update({"accounts": (CHASE,), **overrides})
    ledger.save_status(FeedStatus(**base))  # type: ignore[arg-type]
    return service


def test_coverage_reports_missing_accounts() -> None:
    coverage = sync_status(budget_service([], synced=False)).coverage()
    assert coverage.connected and coverage.connected_accounts == (CHASE,)
    assert coverage.missing_accounts == (ROBINHOOD,)
    assert coverage.problems == ("Robinhood Gold Card is not connected",)


def test_coverage_flags_stale_feeds() -> None:
    old = (MORNING - timedelta(days=3)).isoformat()
    coverage = sync_status(budget_service([], synced=False), last_success=old).coverage()
    assert any("has not refreshed since Oct 11" in problem for problem in coverage.problems)


def test_coverage_flags_relink_and_other_errors() -> None:
    service = budget_service([], synced=False)
    relink = sync_status(service, last_error="ITEM_LOGIN_REQUIRED").coverage()
    assert "Chase Sapphire Preferred needs to be re-linked" in relink.problems
    other = sync_status(service, last_error="INTERNAL_SERVER_ERROR").coverage()
    assert "Chase Sapphire Preferred had a sync error (INTERNAL_SERVER_ERROR)" in other.problems


def test_nothing_is_connected_before_the_first_successful_sync() -> None:
    assert not budget_service([], synced=False).coverage().connected
    never = sync_status(budget_service([], synced=False), last_success=None)
    assert not never.coverage().connected
