"""Regressions from the 14 October 2026 adversarial review."""

from datetime import UTC, datetime
from decimal import Decimal

import pytest

from budget_bot.budget import BudgetService, LimitChange
from budget_bot.categories import Category
from budget_bot.changes import ChangeError, change_defaults, newest_budget_change, undo_defaults
from budget_bot.ledger_memory import MemoryLedger
from budget_bot.tools import (
    undo_kind,
    wants_adjustment,
    wants_budget_change,
    wants_change,
    wants_rule,
)
from tests.helpers import ALICE, budget_service, txn
from tests.unit.test_budget_changes import ask, change, outcome


def test_ordinary_questions_do_not_open_a_write_gate() -> None:
    assert not wants_budget_change("Give me the dining numbers for the last 30 days")
    assert not wants_budget_change("Set aside the dining budget; did we go over 900 this month?")
    assert not wants_budget_change("Add up what we spent on gifts over the last 90 days")
    assert not wants_change("Change my mind: why is dining so high this month?")
    assert not wants_rule("Count on it, we always overspend at Amazon - how much on gifts?")
    assert undo_kind("Undo? What does that even do") is None
    assert not wants_adjustment("Was the Ruth's Chris charge really $100?")


def test_real_requests_still_open_their_gate() -> None:
    assert wants_budget_change("Raise dining to $900")
    assert wants_budget_change("Move $100 from shopping to groceries")
    assert wants_change("Move the Costco charge to gifts")
    assert wants_change("Move that Costco charge to groceries, don't ask")
    assert wants_rule("Always count WGC* as gifts")
    assert wants_adjustment("The Ruth's Chris charge was really $100, someone paid us back $100")
    assert undo_kind("Undo the adjustment") == "adjustment"


def test_thirty_days_cannot_become_a_dining_limit() -> None:
    svc = budget_service()
    reply, llm = ask(
        svc,
        "Give me the dining numbers for the last 30 days",
        [change({"category": "restaurants", "new_limit": 30})],
    )
    assert "did not ask to change the budget" in outcome(llm)["error"]
    assert svc.limits(2026, 10)[Category.DINING] == Decimal("750")
    assert reply.startswith("I couldn't make that change")


def test_a_recategorization_has_to_be_the_merchant_the_person_named() -> None:
    svc = budget_service(
        [
            txn("costco", merchant="Costco", primary="GENERAL_MERCHANDISE"),
            txn("amazon", merchant="Amazon", primary="GENERAL_MERCHANDISE"),
        ]
    )
    _, llm = ask(
        svc,
        "Move the Costco charge to gifts",
        [("search_transactions", {"merchant": "Amazon", "period": "this_month"})],
        [("recategorize_transaction", {"ref": "t1", "category": "gifts_donations"})],
    )
    assert "which charge" in outcome(llm, 1)["error"]
    assert svc.ledger.overrides() == {}


def test_a_transfer_cannot_be_applied_on_one_side_only() -> None:
    svc = budget_service()
    before = svc.budget_total()
    _, llm = ask(
        svc,
        "Move $100 from shopping to groceries",
        [change({"category": "groceries_household", "change_by": 100})],
    )
    assert "both categories" in outcome(llm)["error"]
    assert svc.budget_total() == before


def test_undoing_a_standing_change_from_a_past_month_is_refused() -> None:
    ledger = MemoryLedger()
    march_svc = BudgetService(ledger, ledger, lambda: datetime(2026, 3, 10, 15, tzinfo=UTC))
    change_defaults(march_svc, [LimitChange(Category.DINING, set_to=Decimal("900"))], ALICE)
    october = BudgetService(ledger, ledger, lambda: datetime(2026, 10, 14, 13, tzinfo=UTC))
    assert newest_budget_change(october, 2026, 10) is None
    with pytest.raises(ChangeError, match="past month"):
        undo_defaults(october, ALICE)
    assert october.limits(2026, 3)[Category.DINING] == Decimal("900.00")
    assert october.limits(2026, 10)[Category.DINING] == Decimal("900.00")


def test_an_active_correction_survives_a_long_audit_log() -> None:
    svc = budget_service([txn("t1", "50.00", merchant="Amazon", primary="GENERAL_MERCHANDISE")])
    svc.recategorize("t1", Category.GIFTS, ALICE)
    for _ in range(100):
        svc.change_limits([LimitChange(Category.DINING, change_by=Decimal("1"))], ALICE)
        svc.undo_limit_change(ALICE)
    assert svc.last_correction() == "t1"
    assert svc.undo_recategorization("t1", ALICE) == Category.SHOPPING
