"""Last-exchange memory, charge choice, receipt splits, and repeating charges."""

from datetime import date
from decimal import Decimal

import pytest

from budget_bot.assistant import SYSTEM_PROMPT, Assistant, accepts_offer, clip_reply
from budget_bot.categories import Category
from budget_bot.changes import ChangeError, split_charge
from budget_bot.commands import command_reply
from budget_bot.report import build_report
from budget_bot.tools import choice_reply, listed_charge
from tests.helpers import (
    ALICE,
    MemoryState,
    ScriptedLlm,
    budget_service,
    text_reply,
    tool_reply,
    txn,
)
from tests.unit.test_budget_changes import ask


def test_two_charges_are_listed_and_nothing_is_changed() -> None:
    first = txn("one", "40.00", date(2026, 10, 2), "Target", primary="GENERAL_MERCHANDISE")
    second = txn("two", "80.00", date(2026, 10, 9), "Target", primary="GENERAL_MERCHANDISE")
    svc = budget_service([first, second])
    reply, _ = ask(
        svc,
        "Move the Target charge to gifts",
        [("search_transactions", {"merchant": "Target", "period": "this_month"})],
        [("recategorize_transaction", {"ref": "t1", "category": "gifts_donations"})],
    )
    assert reply == choice_reply(
        [
            {
                "merchant": "Target",
                "amount": "$80.00",
                "date": "2026-10-09",
                "txn_id": "two",
                "amount_value": "80.00",
            },
            {
                "merchant": "Target",
                "amount": "$40.00",
                "date": "2026-10-02",
                "txn_id": "one",
                "amount_value": "40.00",
            },
        ]
    )
    assert svc.ledger.overrides() == {}


def test_a_follow_up_names_the_charge_that_was_listed() -> None:
    first = txn("one", "40.00", date(2026, 10, 2), "Target", primary="GENERAL_MERCHANDISE")
    second = txn("two", "80.00", date(2026, 10, 9), "Target", primary="GENERAL_MERCHANDISE")
    svc = budget_service([first, second])
    state = MemoryState()
    llm = ScriptedLlm(
        [
            tool_reply(("search_transactions", {"merchant": "Target", "period": "this_month"})),
            tool_reply(("recategorize_transaction", {"ref": "t1", "category": "gifts_donations"})),
        ]
    )
    Assistant(svc, llm, None, 30, context=state).answer("Move the Target charge to gifts", ALICE)
    reply = Assistant(svc, ScriptedLlm([]), None, 30, context=state).answer(
        "the October 9 one", ALICE
    )
    assert "Target ($80.00) on 2026-10-09" in reply
    assert svc.ledger.overrides()["two"].category == Category.GIFTS


def test_yes_carries_out_the_previous_offer() -> None:
    svc = budget_service()
    state = MemoryState()
    offer = "Want a closer look at any category to see what's driving the spending?"
    state.save_context({"question": "Where should we focus?", "reply": offer, "charges": []})
    llm = ScriptedLlm([text_reply("Services is the one to look at.")])
    Assistant(svc, llm, None, 30, context=state).answer("Yes", ALICE)
    sent = llm.calls[0]["messages"][0]["content"][0]["text"]
    assert offer in sent and "New message: Yes" in sent
    assert "accepts the offer" in sent
    assert "nobody can answer you in a follow-up" not in SYSTEM_PROMPT
    assert "yes/no confirmation" in SYSTEM_PROMPT


def test_a_saved_reply_keeps_the_closing_offer() -> None:
    offer = "Want a closer look at any category to see what's driving the spending?"
    reply = ("The month is ahead of pace.\n" * 80) + offer
    assert len(reply) > 1200
    saved = clip_reply(reply)
    assert len(saved) <= 1200 and saved.endswith(offer)
    assert accepts_offer("Yes!") and accepts_offer("yes please")
    assert not accepts_offer("yes, but only shopping")


def test_the_previous_exchange_is_shown_to_the_model_but_not_treated_as_the_request() -> None:
    svc = budget_service()
    state = MemoryState()
    state.save_context(
        {"question": "Move the Target charge to gifts", "reply": "Which one?", "charges": []}
    )
    llm = ScriptedLlm([text_reply("Dining is fine.")])
    Assistant(svc, llm, None, 30, context=state).answer("Why is dining up?", ALICE)
    sent = llm.calls[0]["messages"][0]["content"][0]["text"]
    assert "Previous exchange" in sent and "New message: Why is dining up?" in sent
    assert svc.ledger.budget_record("2026-10").revision == 0


def test_a_date_or_amount_picks_one_listed_charge() -> None:
    charges = [
        {"amount_value": "40.00", "date": "2026-10-02", "merchant": "Target"},
        {"amount_value": "80.00", "date": "2026-10-09", "merchant": "Target"},
    ]
    assert listed_charge("the October 9 one", charges) == charges[1]
    assert listed_charge("the $40 one", charges) == charges[0]
    assert listed_charge("the second", charges) == charges[1]
    assert listed_charge("yes", charges) is None


def test_a_receipt_can_count_in_two_categories() -> None:
    target = txn("shop", "100.00", date(2026, 10, 6), "Target", primary="GENERAL_MERCHANDISE")
    svc = budget_service([target])
    split_charge(
        svc,
        "shop",
        ALICE,
        [(Category.GROCERIES, Decimal("40")), (Category.SHOPPING, Decimal("60"))],
    )
    summary = svc.month_summary()
    assert summary.line(Category.GROCERIES).committed == Decimal("40.00")
    assert summary.line(Category.SHOPPING).committed == Decimal("60.00")
    assert summary.committed == Decimal("100.00")


def test_a_split_that_does_not_add_up_is_refused() -> None:
    target = txn("shop", "100.00", date(2026, 10, 6), "Target", primary="GENERAL_MERCHANDISE")
    svc = budget_service([target])
    with pytest.raises(ChangeError, match="add up"):
        split_charge(
            svc,
            "shop",
            ALICE,
            [(Category.GROCERIES, Decimal("40")), (Category.SHOPPING, Decimal("50"))],
        )
    assert svc.ledger.settings_of("SPLIT") == {}


def test_a_merchant_that_repeats_is_named() -> None:
    charges = [
        txn("a", "15.99", date(2026, 8, 3), "NETFLIX.COM", primary="ENTERTAINMENT"),
        txn("b", "15.99", date(2026, 9, 3), "NETFLIX.COM", primary="ENTERTAINMENT"),
        txn("c", "15.99", date(2026, 10, 3), "NETFLIX.COM", primary="ENTERTAINMENT"),
        txn("d", "40.00", date(2026, 10, 4), "Corner Shop", primary="GENERAL_MERCHANDISE"),
    ]
    svc = budget_service(charges)
    names = [name for name, _amount, _count in svc.repeating_charges()]
    assert names == ["NETFLIX.COM"]
    # Recurring merchants stay off the morning scan; `/ask repeats` lists them.
    assert "NETFLIX.COM" not in build_report(svc)
    assert "NETFLIX.COM: $15.99" in (command_reply("repeats", svc) or "")
