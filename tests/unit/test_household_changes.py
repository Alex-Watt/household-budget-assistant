"""Charge adjustments, merchant rules and standing limits: the engine, the rules, and the AI tools."""

from datetime import date, datetime, timedelta
from decimal import Decimal
from typing import Any

import pytest

from budget_bot.assistant import ALREADY_DONE, NOT_DONE, SYSTEM_PROMPT, Assistant
from budget_bot.budget import (
    BudgetError,
    BudgetService,
    LimitChange,
    _standing_defaults,
)
from budget_bot.categories import Category
from budget_bot.changes import (
    ChangeError,
    adjust_charge,
    change_defaults,
    clean_match,
    newest_budget_change,
    remove_rule,
    set_rule,
    undo_adjustment,
    undo_defaults,
    undo_rule_change,
)
from budget_bot.ledger_memory import MemoryLedger
from budget_bot.ports import BudgetConflict
from budget_bot.render import adjustments_text, summary_text
from budget_bot.report import build_report
from budget_bot.tools import (
    TOOL_NAMES,
    summary_data,
    undo_kind,
    wants_adjustment,
    wants_rule,
    wants_rule_removal,
    write_notice,
)
from budget_bot.transactions import (
    Adjustment,
    HouseholdRule,
    Override,
    adjustments_from,
    merchant_matches,
    normalize_match,
    resolve_all,
    rule_for,
    rules_from,
    search_matches,
)
from tests.helpers import (
    ALICE,
    BOB,
    MORNING,
    ScriptedLlm,
    budget_service,
    txn,
)
from tests.unit.test_budget_changes import ask, change, outcome

SHOPPING = {"primary": "GENERAL_MERCHANDISE", "detailed": "GENERAL_MERCHANDISE_ONLINE_MARKETPLACES"}
GIFT = txn(
    "gift",
    "200.00",
    date(2026, 10, 3),
    "WGC*RUTH CHRIS",
    primary="TRANSFER_OUT",
    detailed="TRANSFER_OUT_ACCOUNT_TRANSFER",
)
AMAZON = txn("amz", "50.00", date(2026, 10, 6), "Amazon", **SHOPPING)
WHEN = "2026-10-14T13:00:00+00:00"
OCT = (2026, 10)


def service(*extra: Any) -> BudgetService:
    return budget_service([GIFT, AMAZON, *extra])


def spent(svc: BudgetService, category: Category) -> Decimal:
    return svc.month_summary().line(category).committed


def search_then(*calls: Any) -> list[Any]:
    """The usual two rounds: find the Ruth's Chris charge, then act on it."""
    return [[("search_transactions", {"merchant": "ruth", "period": "this_month"})], list(calls)]


def adjust(**args: Any) -> tuple[str, dict[str, Any]]:
    return ("adjust_charge_amount", {"ref": "t1", **args})


# The engine -----------------------------------------------------------------------------------


def test_an_adjustment_replaces_what_a_charge_counts_for_and_keeps_the_cards_amount() -> None:
    adjustment = Adjustment("gift", Decimal("100.00"), "gift", ALICE, WHEN)
    (item,) = resolve_all([GIFT], {}, {"gift": adjustment})
    assert item.counted and item.category == Category.MISC
    assert item.txn.amount == Decimal("100.00") and item.charged == Decimal("200.00")
    assert GIFT.amount == Decimal("200.00")  # the stored charge itself is never rewritten


def test_without_an_adjustment_nothing_is_different() -> None:
    (item,) = resolve_all([GIFT], {})
    assert item.txn.amount == Decimal("200.00") and item.charged is None


@pytest.mark.parametrize("counted", ["200.00", "250.00", "-1.00"])
def test_an_adjustment_never_raises_a_charge(counted: str) -> None:
    adjustment = Adjustment("gift", Decimal(counted), "", ALICE, WHEN)
    (item,) = resolve_all([GIFT], {}, {"gift": adjustment})
    assert item.txn.amount == Decimal("200.00") and item.charged is None


def test_a_refund_or_an_excluded_record_is_never_adjusted() -> None:
    refund = txn("refund", "-50.00", date(2026, 10, 7), "Amazon", **SHOPPING)
    payment = txn("pay", "-900.00", date(2026, 10, 8), "AUTOMATIC PAYMENT", primary="LOAN_PAYMENTS")
    adjustments = {
        "refund": Adjustment("refund", Decimal("10.00"), "", ALICE, WHEN),
        "pay": Adjustment("pay", Decimal("10.00"), "", ALICE, WHEN),
    }
    items = {i.txn.txn_id: i for i in resolve_all([refund, payment], {}, adjustments)}
    assert items["refund"].txn.amount == Decimal("-50.00") and items["refund"].charged is None
    assert items["pay"].excluded == "payment_or_transfer" and items["pay"].charged is None


def test_an_adjustment_follows_the_purchase_from_pending_to_posted() -> None:
    pending = txn("p1", "200.00", date(2026, 10, 3), "WGC*RUTH CHRIS", pending=True)
    posted = txn("q1", "200.00", date(2026, 10, 4), "WGC*RUTH CHRIS", pending_of="p1")
    adjustment = Adjustment("p1", Decimal("100.00"), "", ALICE, WHEN)  # made while it was pending
    items = {i.txn.txn_id: i for i in resolve_all([pending, posted], {}, {"p1": adjustment})}
    assert items["p1"].excluded is not None  # the pending record is replaced by the posted one
    assert items["q1"].txn.amount == Decimal("100.00") and items["q1"].charged == Decimal("200.00")


def test_an_adjustment_still_follows_when_the_pending_record_is_gone() -> None:
    # Plaid removes the pending record when it posts; only the posted one names it.
    posted = txn("q1", "200.00", date(2026, 10, 4), "WGC*RUTH CHRIS", pending_of="p1")
    adjustment = Adjustment("p1", Decimal("100.00"), "", ALICE, WHEN)
    (item,) = resolve_all([posted], {}, {"p1": adjustment})
    assert item.txn.amount == Decimal("100.00") and item.charged == Decimal("200.00")


def test_a_duplicate_is_found_on_the_cards_amounts_and_counted_once_adjusted() -> None:
    plaid = txn("plaid:g", "200.00", date(2026, 10, 3), "WGC*RUTH CHRIS", feed="plaid:item1")
    csv = txn(
        "csv:g", "200.00", date(2026, 10, 3), "WGC*RUTH CHRIS", feed="csv:chase-sapphire-preferred"
    )
    adjustment = Adjustment("csv:g", Decimal("100.00"), "", ALICE, WHEN)  # made on the CSV copy
    items = resolve_all([plaid, csv], {}, {"csv:g": adjustment})
    counted = [i for i in items if i.counted]
    assert len(counted) == 1 and counted[0].txn.amount == Decimal("100.00")


def test_unreadable_stored_adjustments_are_skipped() -> None:
    found = adjustments_from(
        {
            "a": {"counted": "100.00", "note": "gift", "by": "u", "at": "t"},
            "b": {"counted": ""},  # an adjustment that was undone
            "c": {"counted": "garbage"},
            "d": {"counted": "-5.00"},
            "e": "not a record",  # type: ignore[dict-item]
            "f": {},
        }
    )
    assert (
        list(found) == ["a"]
        and found["a"].counted == Decimal("100.00")
        and found["a"].note == "gift"
    )


def test_household_rules_sit_between_a_correction_and_the_built_in_rules() -> None:
    rule = HouseholdRule("wgc*", Category.GIFTS, ALICE, WHEN)
    (ruled,) = resolve_all([GIFT], {}, None, (rule,))
    assert ruled.category == Category.GIFTS and ruled.provenance == "household_rule:wgc*"
    # A person's correction of this one charge still wins.
    override = Override("gift", Category.PETS, BOB, WHEN)
    (corrected,) = resolve_all([GIFT], {"gift": override}, None, (rule,))
    assert corrected.category == Category.PETS and corrected.provenance == "user"
    # A household rule beats the built-in Costco rule.
    costco = txn("c1", "30.00", date(2026, 10, 5), "COSTCO WHSE", **SHOPPING)
    (built_in,) = resolve_all([costco], {})
    assert built_in.category == Category.GROCERIES
    (house,) = resolve_all(
        [costco], {}, None, (HouseholdRule("costco", Category.PETS, ALICE, WHEN),)
    )
    assert house.category == Category.PETS


def test_a_rule_never_counts_a_payment_or_a_foreign_charge() -> None:
    payment = txn("pay", "-900.00", date(2026, 10, 8), "WGC*PAYMENT", primary="LOAN_PAYMENTS")
    abroad = txn("eur", "20.00", date(2026, 10, 9), "WGC*PARIS", currency="EUR")
    rule = HouseholdRule("wgc*", Category.GIFTS, ALICE, WHEN)
    items = {i.txn.txn_id: i for i in resolve_all([payment, abroad], {}, None, (rule,))}
    assert items["pay"].excluded == "payment_or_transfer"
    assert items["eur"].excluded == "non_usd_currency"


def test_the_most_specific_rule_wins_and_a_tie_goes_to_the_newest() -> None:
    general = HouseholdRule("ruth", Category.GIFTS, ALICE, "2026-10-01T00:00:00+00:00")
    specific = HouseholdRule("wgc*ruth", Category.DINING, ALICE, "2026-09-01T00:00:00+00:00")
    assert rule_for("WGC*RUTH CHRIS", [general, specific]) == specific
    older = HouseholdRule("ruth", Category.PETS, ALICE, "2026-09-01T00:00:00+00:00")
    assert rule_for("WGC*RUTH CHRIS", [older, general]) == general
    assert rule_for("Amazon", [general, specific]) is None


def test_rules_are_matched_on_cleaned_lowercase_text() -> None:
    assert normalize_match("  WGC*Ruth\u200b   Chris ") == "wgc*ruth chris"
    assert normalize_match("a[b]c<d>") == "abcd"  # link-building characters are dropped


def test_unreadable_stored_rules_are_skipped() -> None:
    good = {"match": "WGC*", "category": "gifts_donations", "by": "u", "at": "t"}
    found = rules_from(
        {
            "rules": [
                good,
                {"match": "x", "category": "nope"},
                {"category": "pets"},
                "junk",
                {"match": "", "category": "pets"},
            ]
        }
    )
    assert [(r.match, r.category) for r in found] == [("wgc*", Category.GIFTS)]
    assert rules_from({"rules": "not a list"}) == () and rules_from({}) == ()


def test_standing_limits_apply_from_their_month_onward_in_order() -> None:
    data = {
        "steps": [
            {"from": "2026-10", "limits": {"restaurants": "900.00"}},
            {"from": "2026-12", "limits": {"restaurants": "1000.00", "pets": "300.00"}},
            {"from": "2026-11", "limits": {"shopping": "1600.00"}},
        ]
    }
    assert _standing_defaults(data, "2026-09")[Category.DINING] == Decimal("750")
    assert _standing_defaults(data, "2026-10")[Category.DINING] == Decimal("900.00")
    assert _standing_defaults(data, "2026-11")[Category.SHOPPING] == Decimal("1600.00")
    assert _standing_defaults(data, "2026-12")[Category.DINING] == Decimal("1000.00")
    assert _standing_defaults(data, "2026-12")[Category.PETS] == Decimal("300.00")


def test_unreadable_standing_steps_are_skipped() -> None:
    data = {
        "steps": [
            "junk",
            {"from": "2026-10"},
            {"from": "2026-10", "limits": {"nope": "5"}},
            {"from": "2026-10", "limits": {"pets": "garbage"}},
            {"from": "2026-10", "limits": {"travel": "99999999"}},
            {"from": "2026-10", "limits": {"gifts_donations": "60.00"}},
        ]
    }
    limits = _standing_defaults(data, "2026-10")
    assert limits[Category.GIFTS] == Decimal("60.00") and limits[Category.TRAVEL] == Decimal("240")


# Charge adjustments ---------------------------------------------------------------------------


def test_a_gift_someone_paid_half_of_counts_for_half_everywhere() -> None:
    svc = service()
    assert spent(svc, Category.MISC) == Decimal("200.00")
    result = adjust_charge(
        svc, "gift", ALICE, reimbursed=Decimal("100"), note="gift, $100 paid back"
    )
    assert (result.charged, result.before, result.after) == (
        Decimal("200.00"),
        None,
        Decimal("100.00"),
    )
    summary = svc.month_summary()
    assert summary.line(Category.MISC).committed == Decimal("100.00")
    assert (summary.adjusted_count, summary.adjusted_total) == (1, Decimal("100.00"))
    assert summary.committed == Decimal("150.00")  # the $50 Amazon charge plus the $100


def test_what_a_charge_counts_for_can_be_given_directly() -> None:
    svc = service()
    result = adjust_charge(svc, "gift", ALICE, counted=Decimal("100"))
    assert result.after == Decimal("100.00") and spent(svc, Category.MISC) == Decimal("100.00")


def test_counting_for_the_full_amount_removes_the_adjustment() -> None:
    svc = service()
    adjust_charge(svc, "gift", ALICE, counted=Decimal("100"))
    result = adjust_charge(svc, "gift", ALICE, counted=Decimal("200"))
    assert result.after is None and spent(svc, Category.MISC) == Decimal("200.00")
    assert svc.month_summary().adjusted_count == 0


@pytest.mark.parametrize(
    ("args", "message"),
    [
        ({}, "either what the charge really counts for"),
        ({"counted": Decimal(5), "reimbursed": Decimal(5)}, "either what the charge"),
        ({"reimbursed": Decimal("0")}, "more than \\$0"),
        ({"reimbursed": Decimal("200.01")}, "no more than the \\$200.00"),
        ({"reimbursed": Decimal("-5")}, "more than \\$0"),
        ({"counted": Decimal("200.01")}, "between \\$0 and the \\$200.00"),
        ({"counted": Decimal("-1")}, "between \\$0 and the \\$200.00"),
        ({"counted": Decimal("NaN")}, "not valid"),
        ({"reimbursed": Decimal("1E+30")}, "not valid"),
    ],
)
def test_an_adjustment_must_make_sense_and_otherwise_changes_nothing(
    args: dict[str, Decimal], message: str
) -> None:
    svc = service()
    with pytest.raises(ChangeError, match=message):
        adjust_charge(svc, "gift", ALICE, **args)
    assert svc.ledger.setting("ADJUST", "gift").revision == 0 and svc.ledger.audit_log(5) == []


def test_only_a_counted_purchase_can_be_adjusted() -> None:
    refund = txn("refund", "-50.00", date(2026, 10, 7), "Amazon", **SHOPPING)
    payment = txn("pay", "-900.00", date(2026, 10, 8), "AUTOMATIC PAYMENT", primary="LOAN_PAYMENTS")
    svc = service(refund, payment)
    with pytest.raises(ChangeError, match="not a refund|not counted"):
        adjust_charge(svc, "refund", ALICE, counted=Decimal("10"))
    with pytest.raises(ChangeError, match="not counted"):
        adjust_charge(svc, "pay", ALICE, counted=Decimal("10"))
    with pytest.raises(ChangeError, match="no such transaction"):
        adjust_charge(svc, "missing", ALICE, counted=Decimal("10"))


def test_adjusting_to_what_it_already_counts_for_writes_nothing() -> None:
    svc = service()
    adjust_charge(svc, "gift", ALICE, counted=Decimal("100"))
    revision = svc.ledger.setting("ADJUST", "gift").revision
    again = adjust_charge(svc, "gift", ALICE, counted=Decimal("100"))
    assert not again.changed and svc.ledger.setting("ADJUST", "gift").revision == revision


def test_adjustments_are_audited_and_the_note_is_cleaned_and_bounded() -> None:
    svc = service()
    adjust_charge(svc, "gift", BOB, counted=Decimal("100"), note="x" * 200 + "[evil](http://x)")
    record = svc.ledger.setting("ADJUST", "gift")
    assert len(record.data["note"]) == 60 and "[" not in record.data["note"]
    (entry,) = record.data["history"]
    assert entry["by"] == BOB and entry["from"] == "" and entry["to"] == "100.00"
    audit = svc.ledger.audit_log(1)[0]
    # The audit subject is namespaced so it can never be mistaken for a category correction.
    assert (audit.user_id, audit.action, audit.txn_id) == (BOB, "adjust", "adjust:gift")


def test_undo_walks_back_through_adjustments_newest_first_across_charges() -> None:
    other = txn("other", "80.00", date(2026, 10, 9), "Bistro", primary="FOOD_AND_DRINK")
    svc = service(other)
    moment = [MORNING]

    def tick() -> datetime:
        moment[0] += timedelta(seconds=1)
        return moment[0]

    svc.clock = tick  # every real change happens at a different moment
    adjust_charge(svc, "gift", ALICE, counted=Decimal("150"))
    adjust_charge(svc, "other", ALICE, counted=Decimal("40"))
    adjust_charge(svc, "gift", ALICE, counted=Decimal("100"))
    first = undo_adjustment(svc, ALICE)  # the newest change: gift 100 back to 150
    assert (first.txn_id, first.before, first.after) == (
        "gift",
        Decimal("100.00"),
        Decimal("150.00"),
    )
    second = undo_adjustment(svc, ALICE)  # then the other charge, which was changed after the 150
    assert (second.txn_id, second.after) == ("other", None)
    third = undo_adjustment(svc, ALICE)
    assert (third.txn_id, third.after) == ("gift", None) and spent(svc, Category.MISC) == Decimal(
        "200.00"
    )
    with pytest.raises(ChangeError, match="no charge adjustment to undo"):
        undo_adjustment(svc, ALICE)


class RacingSettings(MemoryLedger):
    """Refuses a number of setting saves as conflicts, to prove changes are retried."""

    def __init__(self) -> None:
        super().__init__()
        self.refuse = 0

    def save_setting(self, *args: Any, **kwargs: Any) -> None:
        if self.refuse:
            self.refuse -= 1
            raise BudgetConflict("busy")
        super().save_setting(*args, **kwargs)


def test_every_kind_of_change_is_retried_when_someone_else_changes_it_first() -> None:
    ledger = RacingSettings()
    svc = budget_service([GIFT, AMAZON], ledger=ledger)
    ledger.refuse = 2
    adjust_charge(svc, "gift", ALICE, counted=Decimal("100"))
    ledger.refuse = 2
    set_rule(svc, "wgc*", Category.GIFTS, ALICE)
    ledger.refuse = 2
    change_defaults(svc, [LimitChange(Category.DINING, set_to=Decimal("900"))], ALICE)
    assert spent(svc, Category.GIFTS) == Decimal(
        "100.00"
    )  # the adjustment and the rule both applied
    assert svc.defaults(*OCT)[Category.DINING] == Decimal("900.00")
    ledger.refuse = 9
    for attempt in (
        lambda: adjust_charge(svc, "gift", ALICE, counted=Decimal("50")),
        lambda: set_rule(svc, "ruth", Category.PETS, ALICE),
        lambda: change_defaults(svc, [LimitChange(Category.PETS, set_to=Decimal("300"))], ALICE),
        lambda: undo_adjustment(svc, ALICE),
        lambda: undo_rule_change(svc, ALICE),
        lambda: undo_defaults(svc, ALICE),
    ):
        ledger.refuse = 9
        with pytest.raises(ChangeError, match="someone else"):
            attempt()


# Merchant rules -------------------------------------------------------------------------------


def test_a_rule_counts_every_matching_charge_in_the_category_past_and_future() -> None:
    last_month = txn(
        "old", "40.00", date(2026, 9, 12), "WGC*SOMEWHERE", primary="GENERAL_MERCHANDISE"
    )
    svc = service(last_month)
    result = set_rule(svc, " WGC* ", Category.GIFTS, ALICE)
    assert (result.match, result.before, result.after) == ("wgc*", None, Category.GIFTS)
    assert (result.affected_count, result.affected_total) == (1, Decimal("200.00"))
    assert spent(svc, Category.GIFTS) == Decimal("200.00") and spent(svc, Category.MISC) == Decimal(
        0
    )
    assert svc.month_summary(2026, 9).line(Category.GIFTS).committed == Decimal(
        "40.00"
    )  # history too
    later = txn("new", "25.00", date(2026, 10, 13), "WGC*ANOTHER", primary="GENERAL_MERCHANDISE")
    svc.ledger.upsert([later])  # a charge that arrives after the rule was made
    assert spent(svc, Category.GIFTS) == Decimal("225.00")


def test_a_persons_correction_of_one_charge_beats_a_rule() -> None:
    svc = service()
    svc.recategorize("gift", Category.DINING, ALICE)
    result = set_rule(svc, "wgc*", Category.GIFTS, ALICE)
    assert result.affected_count == 0  # that charge was already corrected by a person
    assert spent(svc, Category.DINING) == Decimal("200.00") and spent(
        svc, Category.GIFTS
    ) == Decimal(0)


def test_a_recategorization_judges_where_a_charge_is_by_the_rules_too() -> None:
    svc = service()
    set_rule(svc, "wgc*", Category.GIFTS, ALICE)
    before, after = svc.recategorize("gift", Category.GIFTS, ALICE)
    assert before == after == Category.GIFTS and svc.ledger.overrides() == {}


def test_changing_a_rules_category_is_one_rule_not_two() -> None:
    svc = service()
    set_rule(svc, "wgc*", Category.GIFTS, ALICE)
    result = set_rule(svc, "WGC*", Category.PETS, BOB)
    assert (result.before, result.after) == (Category.GIFTS, Category.PETS)
    (rule,) = rules_from(svc.ledger.setting("RULES", "ALL").data)
    assert rule.category == Category.PETS and rule.set_by == BOB
    again = set_rule(svc, "wgc*", Category.PETS, BOB)
    assert not again.changed and svc.ledger.setting("RULES", "ALL").revision == 2


def test_removing_a_rule_puts_its_charges_back() -> None:
    svc = service()
    set_rule(svc, "wgc*", Category.GIFTS, ALICE)
    result = remove_rule(svc, "WGC*", ALICE)
    assert (result.before, result.after) == (Category.GIFTS, None)
    assert (result.affected_count, result.affected_total) == (1, Decimal("200.00"))
    assert spent(svc, Category.MISC) == Decimal("200.00")
    with pytest.raises(ChangeError, match="no merchant rule for that"):
        remove_rule(svc, "wgc*", ALICE)


def test_undo_walks_back_through_rule_changes_one_at_a_time() -> None:
    svc = service()
    set_rule(svc, "wgc*", Category.GIFTS, ALICE)
    set_rule(svc, "wgc*", Category.PETS, ALICE)
    remove_rule(svc, "wgc*", ALICE)
    first = undo_rule_change(svc, ALICE)  # undoes the removal
    assert (first.match, first.before, first.after) == ("wgc*", None, Category.PETS)
    second = undo_rule_change(svc, ALICE)  # undoes the change to pets
    assert (second.before, second.after) == (Category.PETS, Category.GIFTS)
    third = undo_rule_change(svc, ALICE)  # undoes the first rule
    assert (third.before, third.after) == (Category.GIFTS, None)
    assert spent(svc, Category.MISC) == Decimal("200.00")
    with pytest.raises(ChangeError, match="no merchant rule change to undo"):
        undo_rule_change(svc, ALICE)


@pytest.mark.parametrize(
    "text",
    ["", "ab", "abc", "ab*", "***", "1234", "12 34", "x" * 41, "wgc\u00e9x", "a/b/c/d"],
)
def test_a_rule_needs_enough_specific_text(text: str) -> None:
    with pytest.raises(ChangeError, match="at least 4 letters or digits"):
        clean_match(text)


@pytest.mark.parametrize(
    ("text", "kept"),
    [
        ("WGC*", "wgc*"),
        ("  Ruth's   Chris ", "ruth's chris"),
        ("Costco", "costco"),
        ("A&W 12", "a&w 12"),
    ],
)
def test_specific_rule_text_is_kept_in_its_normal_form(text: str, kept: str) -> None:
    # Three letters are enough when they carry the card processor's `*`, as in "WGC*".
    assert clean_match(text) == kept


def test_a_stored_rule_that_is_too_vague_is_not_applied() -> None:
    found = rules_from(
        {
            "rules": [
                {"match": "abc", "category": "gifts_donations"},
                {"match": "wgc*", "category": "gifts_donations"},
            ]
        }
    )
    assert [rule.match for rule in found] == ["wgc*"]


def test_there_is_a_ceiling_on_the_number_of_rules() -> None:
    svc = service()
    for number in range(30):
        set_rule(svc, f"shop {number:02d}", Category.GIFTS, ALICE)
    with pytest.raises(ChangeError, match="already 30 merchant rules"):
        set_rule(svc, "one more", Category.GIFTS, ALICE)
    set_rule(svc, "shop 00", Category.PETS, ALICE)  # changing an existing one is still fine


# Standing limits ------------------------------------------------------------------------------

DINING_900 = [LimitChange(Category.DINING, set_to=Decimal("900"))]


def test_a_standing_change_applies_from_this_month_and_leaves_the_past_alone() -> None:
    svc = service()
    result = change_defaults(svc, DINING_900, ALICE)
    assert (result.year, result.month) == OCT
    assert (result.total_before, result.total_after) == (Decimal("6280"), Decimal("6430.00"))
    for month in ((2026, 10), (2026, 11), (2027, 3)):
        assert svc.limits(*month)[Category.DINING] == Decimal("900.00")
    assert svc.limits(2026, 9)[Category.DINING] == Decimal("750")  # earlier months keep theirs
    assert svc.budget_total(2026, 9) == Decimal("6280")


def test_a_standing_change_can_start_next_month() -> None:
    svc = service()
    change_defaults(svc, DINING_900, ALICE, 2026, 11)
    assert svc.limits(*OCT)[Category.DINING] == Decimal("750")
    assert svc.limits(2026, 11)[Category.DINING] == Decimal("900.00")
    with pytest.raises(BudgetError, match="this month's or next month's"):
        change_defaults(svc, DINING_900, ALICE, 2026, 12)
    with pytest.raises(BudgetError, match="this month's or next month's"):
        change_defaults(svc, DINING_900, ALICE, 2026, 9)


def test_a_one_off_change_still_wins_over_a_standing_one_and_is_judged_against_it() -> None:
    svc = service()
    change_defaults(svc, DINING_900, ALICE)
    svc.change_limits([LimitChange(Category.DINING, set_to=Decimal("1000"))], BOB)
    summary = svc.month_summary()
    assert summary.line(Category.DINING).cap == Decimal("1000.00")
    assert summary.line(Category.DINING).usual == Decimal(
        "900.00"
    )  # "usually" follows the standing one
    assert summary.limits_changed == (Category.DINING,)
    # Setting the month back to the standing limit stores no one-off change at all.
    svc.change_limits([LimitChange(Category.DINING, set_to=Decimal("900"))], BOB)
    assert svc.ledger.budget_record("2026-10").limits == {}
    assert svc.month_summary().limits_changed == ()


def test_a_standing_change_is_not_reported_as_a_change_to_the_month() -> None:
    svc = service()
    change_defaults(svc, DINING_900, ALICE)
    assert svc.month_summary().limits_changed == ()
    assert "Limits changed" not in build_report(svc)
    assert svc.month_summary().default_budget == Decimal("6430.00")


def test_an_unchanged_standing_request_writes_nothing() -> None:
    svc = service()
    result = change_defaults(svc, [LimitChange(Category.DINING, set_to=Decimal("750"))], ALICE)
    assert not result.changed and svc.ledger.setting("DEFAULTS", "LIMITS").revision == 0


def test_standing_changes_are_validated_like_one_off_ones() -> None:
    svc = service()
    for bad in (
        [LimitChange(Category.DINING, set_to=Decimal("0.5"))],
        [LimitChange(Category.DINING, set_to=Decimal("20001"))],
        [LimitChange(Category.DINING)],
        [],
    ):
        with pytest.raises(BudgetError):
            change_defaults(svc, bad, ALICE)
    assert svc.ledger.setting("DEFAULTS", "LIMITS").revision == 0


def test_undo_takes_back_the_latest_standing_change_for_every_month_it_applied_to() -> None:
    svc = service()
    change_defaults(svc, DINING_900, ALICE)
    change_defaults(svc, [LimitChange(Category.PETS, set_to=Decimal("300"))], BOB)
    first = undo_defaults(svc, ALICE)
    assert [(m.category, m.before, m.after) for m in first.moves] == [
        (Category.PETS, Decimal("300.00"), Decimal("255"))
    ]
    assert svc.limits(2026, 11)[Category.PETS] == Decimal("255")
    assert svc.limits(2026, 11)[Category.DINING] == Decimal("900.00")
    undo_defaults(svc, ALICE)
    assert svc.limits(2026, 11)[Category.DINING] == Decimal("750")
    with pytest.raises(ChangeError, match="no standing limit change to undo"):
        undo_defaults(svc, ALICE)


def test_the_newest_kind_of_budget_change_is_the_one_an_undo_takes_back() -> None:
    svc = service()
    assert newest_budget_change(svc, *OCT) is None
    change_defaults(svc, DINING_900, ALICE)
    assert newest_budget_change(svc, *OCT) == "default"
    svc.change_limits([LimitChange(Category.SHOPPING, set_to=Decimal("1800"))], ALICE)
    assert newest_budget_change(svc, *OCT) == "month"
    svc.undo_limit_change(ALICE)
    assert newest_budget_change(svc, *OCT) == "default"


def test_there_is_a_ceiling_on_standing_changes_because_none_are_ever_dropped() -> None:
    svc = service()
    for number in range(60):
        change_defaults(svc, [LimitChange(Category.DINING, set_to=Decimal(800 + number))], ALICE)
    with pytest.raises(ChangeError, match="too many standing limit changes"):
        change_defaults(svc, [LimitChange(Category.DINING, set_to=Decimal(900))], ALICE)
    assert svc.defaults(*OCT)[Category.DINING] == Decimal("859.00")  # nothing was lost or changed


def test_standing_changes_are_audited() -> None:
    svc = service()
    change_defaults(svc, DINING_900, BOB)
    audit = svc.ledger.audit_log(1)[0]
    assert (audit.user_id, audit.action, audit.txn_id) == (BOB, "default_change", "default:2026-10")
    assert "restaurants=900.00" in audit.to_category


# The AI tools ---------------------------------------------------------------------------------

REALLY_100 = "The Ruth's Chris charge was really $100, someone paid us back $100"


def test_telling_the_ai_a_charge_was_really_less_changes_what_it_counts_for() -> None:
    svc = service()
    reply, llm = ask(
        svc, REALLY_100, *search_then(adjust(reimbursed_amount=100, note="gift, $100 paid back"))
    )
    assert reply == (
        "Adjusted WGC*RUTH CHRIS on 2026-10-03: the card shows $200.00 and it now counts for "
        "$100.00 (it counted for $200.00). "
        "Ask me to undo the last adjustment to put it back."
    )
    # The model's note is kept with the record but never repeated in the notice.
    assert "gift" not in reply and svc.ledger.setting("ADJUST", "gift").data["note"]
    assert len(llm.calls) == 2  # the model is not asked again after the change
    assert spent(svc, Category.MISC) == Decimal("100.00")
    # The next morning's report says so, without naming the merchant.
    report = build_report(svc)
    assert "1 charge counted $100.00 less than the card shows." in report
    assert "RUTH" not in report and "of $6,280.00" in report


@pytest.mark.parametrize(
    ("question", "args"),
    [
        ("Count the Ruth's Chris charge as $100", {"counted_amount": 100}),
        ("Can you change the Ruth's Chris charge to $100?", {"counted_amount": 100}),
        ("We only paid $100 for the Ruth's Chris charge", {"counted_amount": 100}),
        ("Ruth's Chris was split, someone reimbursed us $100", {"reimbursed_amount": 100}),
        ("The $200 charge at Ruth's Chris was actually $100", {"counted_amount": 100}),
    ],
)
def test_natural_ways_of_saying_so_are_understood(question: str, args: dict[str, Any]) -> None:
    svc = service()
    reply, _ = ask(svc, question, *search_then(adjust(**args)))
    assert reply.startswith("Adjusted WGC*RUTH CHRIS on 2026-10-03:")
    assert spent(svc, Category.MISC) == Decimal("100.00")


@pytest.mark.parametrize(
    "question",
    [
        "Was the Ruth's Chris charge really $100?",
        "Why was the Ruth's Chris charge $200?",
        "What if the Ruth's Chris charge was really $100?",
        "Should we count the Ruth's Chris charge as $100?",
        "Do not count the Ruth's Chris charge as $100",
        "The Ruth's Chris charge was really less",  # no amount
        "How much did Ruth's Chris cost, really?",
        "Tell me about the Ruth's Chris charge, it was $200",
    ],
)
def test_questions_and_vague_remarks_never_adjust_anything(question: str) -> None:
    svc = service()
    reply, llm = ask(svc, question, *search_then(adjust(counted_amount=100)))
    assert "did not ask to adjust" in outcome(llm, 1)["error"]
    assert reply.startswith("I couldn't make that change")
    assert svc.ledger.settings_of("ADJUST") == {}


@pytest.mark.parametrize(
    ("question", "args", "complaint"),
    [
        # The model may not choose the amount...
        (REALLY_100, {"reimbursed_amount": 150}, "not one the user wrote"),
        (REALLY_100, {"counted_amount": 120}, "not one the user wrote"),
        # ...or use both, or neither.
        (REALLY_100, {"reimbursed_amount": 100, "counted_amount": 100}, "either counted_amount"),
        (REALLY_100, {}, "either counted_amount"),
        (REALLY_100, {"reimbursed_amount": 0}, "positive"),
        (REALLY_100, {"reimbursed_amount": True}, "must be a number"),
        (REALLY_100, {"reimbursed_amount": 100, "note": 5}, "note must be text"),
        # The charge has to be the one the person named.
        (
            "That charge was really $100, we got $100 back",
            {"reimbursed_amount": 100},
            "which charge",
        ),
        # An amount larger than the charge is refused by the rules, with a reason people can read.
        (
            "The Ruth's Chris charge was really $300, we got $300 back",
            {"reimbursed_amount": 300},
            "no more than",
        ),
    ],
)
def test_the_model_may_not_invent_the_amount_or_the_charge(
    question: str, args: dict[str, Any], complaint: str
) -> None:
    svc = service()
    reply, llm = ask(svc, question, *search_then(adjust(**args)))
    assert complaint in outcome(llm, 1)["error"]
    assert svc.ledger.settings_of("ADJUST") == {} and reply.startswith(
        "I couldn't make that change"
    )


def test_a_reference_the_model_never_searched_for_is_refused() -> None:
    svc = service()
    _, llm = ask(svc, REALLY_100, [adjust(reimbursed_amount=100)])  # no search first
    assert "ref must come from search_transactions" in outcome(llm)["error"]
    assert svc.ledger.settings_of("ADJUST") == {}


def test_a_refused_adjustment_that_people_can_understand_is_passed_on() -> None:
    svc = service()
    reply, _ = ask(
        svc,
        "The Ruth's Chris charge was really $300, we got $300 back",
        *search_then(adjust(reimbursed_amount=300)),
    )
    assert reply == NOT_DONE.format(
        reason=" (the amount paid back must be more than $0 and no more than the $200.00 charged)"
    )


def test_a_redelivered_adjustment_is_not_made_twice() -> None:
    svc = service()
    first, _ = ask(svc, REALLY_100, *search_then(adjust(reimbursed_amount=100)), request_id="r1")
    llm = ScriptedLlm([])
    again = Assistant(svc, llm, None, 30).answer(REALLY_100, ALICE, "r1")
    assert again == ALREADY_DONE + first and llm.calls == []
    assert spent(svc, Category.MISC) == Decimal("100.00")


def test_undoing_an_adjustment_asks_for_it_by_name() -> None:
    svc = service()
    ask(svc, REALLY_100, *search_then(adjust(reimbursed_amount=100)))
    reply, llm = ask(svc, "Undo that", [("undo_last_charge_adjustment", {})])
    assert "did not ask to undo a charge adjustment" in outcome(llm)["error"]
    assert spent(svc, Category.MISC) == Decimal("100.00")
    reply, _ = ask(svc, "Undo the adjustment", [("undo_last_charge_adjustment", {})])
    assert reply == (
        "Undid the adjustment on WGC*RUTH CHRIS: it counts for $200.00 again (card: $200.00)."
    )
    assert spent(svc, Category.MISC) == Decimal("200.00")


def test_a_search_shows_the_cards_amount_next_to_what_the_charge_counts_for() -> None:
    svc = service()
    adjust_charge(svc, "gift", ALICE, counted=Decimal("100"))
    row = svc.search(date(2026, 10, 1), date(2026, 10, 31), "ruth").rows[0]
    assert (row.amount, row.charged) == (Decimal("100.00"), Decimal("200.00"))
    fields = summary_data(svc.month_summary(), svc.coverage())
    assert fields["charges_counted_for_less_than_the_card_shows"] == 1
    assert fields["counted_for_less_by"] == "$100.00"


# Merchant rules through the AI ----------------------------------------------------------------

ALWAYS_GIFTS = "Always count WGC* as gifts"


def rule_call(**args: Any) -> tuple[str, dict[str, Any]]:
    return ("set_merchant_rule", {"merchant": "WGC*", "category": "gifts_donations", **args})


def test_a_standing_instruction_makes_a_rule_and_says_what_it_moved() -> None:
    svc = service()
    reply, llm = ask(svc, ALWAYS_GIFTS, [rule_call()])
    assert reply == (
        'From now on, charges from "wgc*" count as Gifts and donations. '
        "This month that moves 1 charge totalling $200.00. "
        "Earlier months follow the rule too. "
        "Ask me to undo the last rule change to put it back."
    )
    assert len(llm.calls) == 1 and spent(svc, Category.GIFTS) == Decimal("200.00")


@pytest.mark.parametrize(
    "question",
    [
        "Count WGC* as gifts",  # nothing says it is permanent
        "Should we always count WGC* as gifts?",
        "Why do we always see WGC* in gifts?",
        "Do not always count WGC* as gifts",
        "What is the rule for WGC* always?",
    ],
)
def test_a_rule_needs_a_standing_instruction_not_a_question_or_a_one_off(question: str) -> None:
    svc = service()
    _, llm = ask(svc, question, [rule_call()])
    assert "did not ask for a standing merchant rule" in outcome(llm)["error"]
    assert svc.ledger.setting("RULES", "ALL").revision == 0


@pytest.mark.parametrize(
    ("question", "args", "complaint"),
    [
        (ALWAYS_GIFTS, {"merchant": "ACME"}, "not in what the user wrote"),
        (ALWAYS_GIFTS, {"category": "pets"}, "did not name Pets"),
        (ALWAYS_GIFTS, {"merchant": 5}, "merchant must be text"),
        (ALWAYS_GIFTS, {"category": "nonsense"}, "category must be"),
        ("Always count WGC as gifts", {"merchant": "WGC"}, "at least 4 letters or digits"),
        ("Always count 1234 as gifts", {"merchant": "1234"}, "at least 4 letters or digits"),
    ],
)
def test_a_rule_must_use_the_merchant_and_category_the_person_wrote(
    question: str, args: dict[str, Any], complaint: str
) -> None:
    svc = service()
    reply, llm = ask(svc, question, [rule_call(**args)])
    assert complaint in outcome(llm)["error"]
    assert svc.ledger.setting("RULES", "ALL").revision == 0
    assert reply.startswith("I couldn't make that change")


def test_a_rule_the_rules_refuse_explains_why_in_plain_words() -> None:
    svc = service()
    reply, _ = ask(svc, "Always count WGC as gifts", [rule_call(merchant="WGC")])
    # That text is too short for a rule, and the refusal is worded for people.
    assert reply.startswith(
        "I couldn't make that change (a merchant rule needs at least 4 letters or digits"
    )


def test_removing_and_undoing_a_rule_through_the_ai() -> None:
    svc = service()
    ask(svc, ALWAYS_GIFTS, [rule_call()])
    _, llm = ask(svc, "Remove WGC*", [("remove_merchant_rule", {"merchant": "WGC*"})])
    assert "did not ask to remove a merchant rule" in outcome(llm)["error"]
    reply, _ = ask(svc, "Remove the WGC* rule", [("remove_merchant_rule", {"merchant": "WGC*"})])
    assert reply == (
        'Removed the rule for "wgc*" (it counted charges as Gifts and donations). '
        "This month that moves 1 charge totalling $200.00. Earlier months change back too."
    )
    assert spent(svc, Category.MISC) == Decimal("200.00")
    reply, _ = ask(svc, "Undo the last rule change", [("undo_last_rule_change", {})])
    assert (
        reply == 'Undid the last merchant rule change: "wgc*" counts as Gifts and donations again.'
    )
    assert spent(svc, Category.GIFTS) == Decimal("200.00")
    _, llm = ask(svc, "Undo that", [("undo_last_rule_change", {})])
    assert "did not ask to undo a merchant rule change" in outcome(llm)["error"]


# Standing limits through the AI ---------------------------------------------------------------


def permanent(**changes: Any) -> tuple[str, dict[str, Any]]:
    return ("change_monthly_budget", {"from_now_on": True, "changes": [changes]})


def test_from_now_on_makes_a_limit_permanent() -> None:
    svc = service()
    reply, _ = ask(
        svc,
        "Raise dining to $900 from now on",
        [permanent(category="restaurants", new_limit=900)],
    )
    assert reply == (
        "From October 2026 on, these limits change every month:\n"
        "- Restaurants, takeout and coffee: $750.00 to $900.00\n"
        "The monthly budget from then on is now $6,430.00 (was $6,280.00). "
        "Ask me to undo the last budget change to put it back."
    )
    assert svc.limits(2026, 11)[Category.DINING] == Decimal("900.00")
    assert svc.ledger.budget_record("2026-10").revision == 0  # no one-off change was stored


def test_permanent_needs_the_person_to_say_so() -> None:
    svc = service()
    _, llm = ask(
        svc, "Raise dining to $900 this month", [permanent(category="restaurants", new_limit=900)]
    )
    assert "did not say this is permanent" in outcome(llm)["error"]
    _, llm = ask(
        svc,
        "Raise dining to $900 every month",
        [
            (
                "change_monthly_budget",
                {"from_now_on": "yes", "changes": [{"category": "restaurants", "new_limit": 900}]},
            )
        ],
    )
    assert "from_now_on must be true or false" in outcome(llm)["error"]
    assert svc.ledger.setting("DEFAULTS", "LIMITS").revision == 0


def test_a_permanent_word_does_not_make_a_one_month_request_permanent() -> None:
    svc = service()
    # "always" appears, but the person asked for this month only and the model said so too.
    reply, _ = ask(
        svc,
        "Raise dining to $900 this month, I always forget",
        [change({"category": "restaurants", "new_limit": 900})],
    )
    assert reply.startswith("Changed the October 2026 budget:")
    assert svc.limits(2026, 11)[Category.DINING] == Decimal("750")


def test_a_permanent_change_can_start_next_month() -> None:
    svc = service()
    reply, _ = ask(
        svc,
        "Set dining to $900 every month, starting next month",
        [permanent(category="restaurants", new_limit=900)],
    )
    assert reply.startswith("From November 2026 on, these limits change every month:")
    assert svc.limits(*OCT)[Category.DINING] == Decimal("750")
    assert svc.limits(2026, 11)[Category.DINING] == Decimal("900.00")


def test_one_undo_takes_back_whichever_budget_change_is_newest() -> None:
    svc = service()
    ask(svc, "Raise dining to $900 from now on", [permanent(category="restaurants", new_limit=900)])
    ask(
        svc,
        "Raise shopping to $1,800 this month",
        [change({"category": "shopping", "new_limit": 1800})],
    )
    reply, _ = ask(svc, "Undo the budget change", [("undo_last_budget_change", {})])
    assert reply.startswith("Undid the last budget change for October 2026:\n- Shopping:")
    reply, _ = ask(svc, "Undo the budget change", [("undo_last_budget_change", {})])
    assert reply.startswith("Undid the latest standing limit change (from October 2026):")
    assert svc.limits(2026, 11)[Category.DINING] == Decimal("750")


# Recognizing what was said ---------------------------------------------------------------------


@pytest.mark.parametrize(
    "question",
    [
        REALLY_100,
        "Count the Ruth's Chris charge as $100",
        "Can you change the Ruth's Chris charge to $100?",
        "Please adjust that charge to $100",
        "It was actually $100, we got $100 back",
        "Someone gave us $100 back for the gift",
        "We only paid $100 for the Ruth's Chris charge",
    ],
)
def test_adjustments_are_recognized(question: str) -> None:
    assert wants_adjustment(question)


@pytest.mark.parametrize(
    "question",
    [
        "Was the Ruth's Chris charge really $100?",
        "Why was it $200?",
        "Should we count it as $100?",
        "Do not count it as $100",
        "Count the Ruth's Chris charge as less",
        "How much is $100 really worth?",
        "What did we spend at Ruth's Chris?",
        "Ruth's Chris was split, so $100 is ours",  # "split" alone is too ambiguous
        "The Ruth's Chris charge was really $100?",  # a question mark makes it a question
        "I wonder if the Ruth's Chris charge was really $100",
        "Maybe the Ruth's Chris charge was actually $100",
        "Ruth's Chris wasn't really $100",
        "If someone paid us back $100 would it be less?",
        "Please adjust that charge, $100",  # no amount to count it as, and no reason given
        "",
    ],
)
def test_ordinary_remarks_are_not_adjustments(question: str) -> None:
    assert not wants_adjustment(question)


@pytest.mark.parametrize(
    ("question", "rule", "removal"),
    [
        ("Always count WGC* as gifts", True, False),
        ("From now on put Ruth's Chris under restaurants", True, False),
        ("Can you always file Costco gas under transportation?", True, False),
        ("Every time we see WGC* it is a gift", True, False),
        ("Remove the WGC* rule", False, True),
        ("Please delete the rule for Costco", False, True),
        ("Count WGC* as gifts", False, False),
        ("Should we always count WGC* as gifts?", False, False),
        ("Why is WGC* always in miscellaneous?", False, False),
        ("Do not always count it as gifts", False, False),
        ("Please always count WGC* as gifts", True, False),
        ("Going forward treat Costco as groceries", True, False),
        ("I think we always count WGC* as gifts", False, False),  # not an instruction
        ("WGC* is always a gift", False, False),
        ("Remove WGC*", False, False),
        ("What rules do we have?", False, False),
    ],
)
def test_rules_and_their_removal_are_recognized(question: str, rule: bool, removal: bool) -> None:
    assert wants_rule(question) is rule
    assert wants_rule_removal(question) is removal


@pytest.mark.parametrize(
    ("question", "kind"),
    [
        ("Undo that", "correction"),
        ("Undo the last category change", "correction"),
        ("Please revert it", "correction"),
        ("Undo the budget change", "budget"),
        ("Undo the dining limit", "budget"),
        ("Roll back the default", "budget"),
        ("Undo the adjustment", "adjustment"),
        ("Undo the reimbursement", "adjustment"),
        ("Undo the last rule change", "rule"),
        ("Undo the budget adjustment", "budget"),  # earlier kinds win
        ("Undo the rule adjustment", "rule"),
        # Only the words right after the verb say what is undone, not a later explanation.
        ("Undo that correction, it counted as groceries", "correction"),
        ("Undo that, the budget is fine now", "correction"),
        ("Undo that because it was counted as a gift", "correction"),
        ("Undo that - it was the budget change that was wrong", "correction"),
        ("Undo the rule because the budget looked off", "rule"),
        ("Revert the adjustment, the rule was right", "adjustment"),
        ("Do not undo that", None),
        ("What does undo do?", None),
        ("", None),
    ],
)
def test_an_undo_request_is_matched_to_the_kind_of_thing_it_undoes(
    question: str, kind: str | None
) -> None:
    assert undo_kind(question) == kind


def test_the_older_undo_refuses_when_the_request_is_about_something_else() -> None:
    svc = service()
    svc.recategorize("amz", Category.GIFTS, ALICE)
    for question, hint in (
        ("Undo the budget change", "budget change"),
        ("Undo the adjustment", "charge adjustment"),
        ("Undo the last rule change", "merchant rule"),
    ):
        _, llm = ask(svc, question, [("undo_last_recategorization", {})])
        assert hint in outcome(llm)["error"], question
    assert "amz" in svc.ledger.overrides()


def test_the_new_tools_exist_and_the_assistant_is_told_how_to_use_them() -> None:
    assert {
        "adjust_charge_amount",
        "undo_last_charge_adjustment",
        "set_merchant_rule",
        "remove_merchant_rule",
        "undo_last_rule_change",
    } <= TOOL_NAMES
    for name in ("adjust_charge_amount", "set_merchant_rule", "from_now_on"):
        assert name in SYSTEM_PROMPT


@pytest.mark.parametrize(
    ("result", "words"),
    [
        (
            {
                "undone": True,
                "merchant": None,
                "card_charge": None,
                "now_counted_as": "$200.00",
            },
            "Undid the adjustment on that charge: it counts for $200.00 again (card: its charge).",
        ),
        (
            {
                "changed": False,
                "merchant": "Bistro",
                "card_charge": "$80.00",
                "now_counted_as": "$40.00",
                "was_counted_as": "$40.00",
            },
            "No change was needed: Bistro already counts for $40.00.",
        ),
        (
            {
                "changed": False,
                "rule": "wgc*",
                "rule_before": "Gifts and donations",
                "rule_after": "Gifts and donations",
            },
            'No change was needed: charges from "wgc*" already count as Gifts and donations.',
        ),
        (
            {
                "undone": True,
                "rule": "wgc*",
                "rule_before": "Gifts and donations",
                "rule_after": None,
            },
            'Undid the last merchant rule change: there is no rule for "wgc*" now.',
        ),
        (
            {
                "changed": True,
                "rule": "wgc*",
                "rule_before": None,
                "rule_after": "Gifts and donations",
                "this_month_charges": 3,
                "this_month_total": "$90.00",
            },
            'From now on, charges from "wgc*" count as Gifts and donations. '
            "This month that moves 3 charges totalling $90.00.",
        ),
        (
            {
                "changed": True,
                "rule": "wgc*",
                "rule_before": None,
                "rule_after": "Gifts and donations",
                "this_month_charges": 0,
                "this_month_total": "$0.00",
            },
            'From now on, charges from "wgc*" count as Gifts and donations. '
            "No charge this month is affected.",
        ),
        (
            {
                "changed": False,
                "standing": True,
                "month": "2026-10",
                "changes": [],
                "budget_was": "$1",
                "budget_now": "$1",
            },
            "No change was needed: those limits already apply from October 2026 on.",
        ),
    ],
)
def test_the_notices_for_the_new_changes(result: dict[str, Any], words: str) -> None:
    assert write_notice(result).startswith(words)


def test_the_summary_and_the_report_say_how_many_charges_were_adjusted() -> None:
    svc = service()
    assert adjustments_text(svc.month_summary()) is None
    adjust_charge(svc, "gift", ALICE, counted=Decimal("100"))
    summary = svc.month_summary()
    assert adjustments_text(summary) == "1 charge counted for $100.00 less than the card shows."
    assert "1 charge counted for $100.00 less" in summary_text(summary, svc.coverage())
    other = txn("other", "80.00", date(2026, 10, 9), "Bistro", primary="FOOD_AND_DRINK")
    svc.ledger.upsert([other])
    adjust_charge(svc, "other", ALICE, counted=Decimal("30"))
    svc.change_limits(DINING_900, ALICE)
    text = adjustments_text(svc.month_summary()) or ""
    assert text.splitlines()[0].startswith("Limits changed this month:")
    assert text.splitlines()[1] == "2 charges counted for $150.00 less than the card shows."


# Fixes from the second review ------------------------------------------------------------------


def test_a_pending_charge_cannot_be_adjusted() -> None:
    pending = txn(
        "pend", "80.00", date(2026, 10, 9), "Bistro", pending=True, primary="FOOD_AND_DRINK"
    )
    svc = service(pending)
    with pytest.raises(ChangeError, match="still pending"):
        adjust_charge(svc, "pend", ALICE, counted=Decimal("40"))
    assert svc.ledger.settings_of("ADJUST") == {}


def test_adjusting_a_charge_leaves_its_category_correction_and_undo_alone() -> None:
    svc = service()
    svc.recategorize("gift", Category.PETS, ALICE)
    adjust_charge(svc, "gift", ALICE, counted=Decimal("100"))
    svc.undo_recategorization("gift", ALICE)  # takes back the correction, not the adjustment
    assert "gift" not in svc.ledger.overrides()
    assert spent(svc, Category.MISC) == Decimal("100.00")


def test_a_standing_change_that_starts_later_is_not_the_newest_change_for_this_month() -> None:
    svc = service()
    moment = [MORNING]

    def tick() -> datetime:
        moment[0] += timedelta(seconds=1)
        return moment[0]

    svc.clock = tick  # each change at its own moment, so "newest" has an answer
    svc.change_limits([LimitChange(Category.SHOPPING, set_to=Decimal("1800"))], ALICE)
    change_defaults(svc, DINING_900, ALICE, 2026, 11)  # standing from November, and newer
    assert newest_budget_change(svc, *OCT) == "month"
    assert newest_budget_change(svc, 2026, 11) == "default"


def test_undoing_a_standing_change_removes_exactly_the_last_step() -> None:
    svc = service()
    change_defaults(svc, DINING_900, ALICE)
    change_defaults(svc, [LimitChange(Category.DINING, set_to=Decimal("1000"))], ALICE, 2026, 11)
    undo_defaults(svc, ALICE)
    assert svc.defaults(*OCT)[Category.DINING] == Decimal("900.00")
    assert svc.defaults(2026, 11)[Category.DINING] == Decimal("900.00")
    undo_defaults(svc, ALICE)
    assert svc.defaults(2026, 11)[Category.DINING] == Decimal("750")


TWIN = txn("twin", "200.00", date(2026, 10, 5), "RUTH CHRIS STEAK HOUSE", primary="FOOD_AND_DRINK")
STATED_200 = "That $200 charge was really $100, we got $100 back"


def test_a_stated_amount_names_the_charge_when_only_one_found_has_it() -> None:
    svc = service()
    reply, _ = ask(svc, STATED_200, *search_then(adjust(reimbursed_amount=100)))
    assert reply.startswith("Adjusted WGC*RUTH CHRIS on 2026-10-03:")


def test_a_stated_amount_does_not_name_a_charge_when_two_found_have_it() -> None:
    svc = service(TWIN)
    _, llm = ask(svc, STATED_200, *search_then(adjust(reimbursed_amount=100)))
    assert "which charge" in outcome(llm, 1)["error"]
    assert svc.ledger.settings_of("ADJUST") == {}


def test_finding_a_charge_with_a_generic_word_does_not_name_it() -> None:
    shop = txn("shop", "60.00", date(2026, 10, 8), "THE GIFT SHOP", primary="GENERAL_MERCHANDISE")
    svc = service(shop)
    _, llm = ask(
        svc,
        "The charge was really $30, we got $30 back",
        [("search_transactions", {"merchant": "the", "period": "this_month"})],
        [adjust(reimbursed_amount=30)],
    )
    assert "which charge" in outcome(llm, 1)["error"]
    assert svc.ledger.settings_of("ADJUST") == {}


@pytest.mark.parametrize(
    ("question", "args", "complaint"),
    [
        (
            "Ruth's Chris was split, someone reimbursed us $100",
            {"counted_amount": 100},
            "so use reimbursed_amount",
        ),
        (
            "Count the Ruth's Chris charge as $100",
            {"reimbursed_amount": 100},
            "so use counted_amount",
        ),
        (
            "Someone paid us back $200 for the Ruth's Chris charge",
            {"reimbursed_amount": 200},
            "paid back in full",
        ),
    ],
)
def test_the_model_may_not_swap_how_an_amount_is_meant(
    question: str, args: dict[str, Any], complaint: str
) -> None:
    svc = service()
    _, llm = ask(svc, question, *search_then(adjust(**args)))
    assert complaint in outcome(llm, 1)["error"]
    assert svc.ledger.settings_of("ADJUST") == {}


def test_a_charge_paid_back_in_full_counts_for_nothing_when_the_person_says_so() -> None:
    svc = service()
    question = "Someone paid us back all $200 for the Ruth's Chris charge"
    reply, _ = ask(svc, question, *search_then(adjust(reimbursed_amount=200)))
    assert reply.startswith("Adjusted WGC*RUTH CHRIS on 2026-10-03:")
    assert spent(svc, Category.MISC) == Decimal("0.00")


def test_a_standing_change_says_when_a_one_off_limit_still_wins() -> None:
    svc = service()
    svc.change_limits([LimitChange(Category.DINING, set_to=Decimal("800"))], ALICE)
    reply, _ = ask(
        svc,
        "Raise dining to $900 from now on",
        [permanent(category="restaurants", new_limit=900)],
    )
    assert (
        "October 2026's own limit still applies for Restaurants, takeout and coffee: $800.00."
        in reply
    )
    assert svc.limits(*OCT)[Category.DINING] == Decimal("800.00")
    assert svc.limits(2026, 11)[Category.DINING] == Decimal("900.00")


def test_a_standing_change_with_no_one_off_limit_says_nothing_about_one() -> None:
    svc = service()
    reply, _ = ask(
        svc,
        "Raise dining to $900 from now on",
        [permanent(category="restaurants", new_limit=900)],
    )
    assert "own limit still applies" not in reply


def test_a_rule_is_found_in_a_long_request() -> None:
    # The merchant is well past the first 80 characters, which the match used to be cut at.
    svc = service()
    question = "Always count " + "really " * 40 + "WGC* as gifts"
    reply, _ = ask(svc, question, [rule_call()])
    assert reply.startswith('From now on, charges from "wgc*" count as Gifts and donations.')


# Apostrophes and symbols in how a merchant is written ------------------------------------------
# Found by the live model: "the Ruth's Chris charge" searched for "Ruth's Chris" and found nothing,
# because the bank wrote WGC*RUTH CHRIS.


@pytest.mark.parametrize(
    ("needle", "merchant", "found"),
    [
        ("Ruth's Chris", "WGC*RUTH CHRIS", True),
        ("Ruth\u2019s Chris", "WGC*RUTH CHRIS", True),  # a phone's curly apostrophe
        ("ruth chris", "RUTH'S CHRIS STEAK HOUSE", True),
        ("chris ruth", "WGC*RUTH CHRIS", True),  # every word, in any order
        ("amazon", "AMAZON MKTP US", True),  # plain text as typed still works
        ("macys", "MACY'S #123", True),  # the bank kept the possessive, the person did not
        ("macy's", "MACYS #123", True),  # and the other way round
        ("wgc*", "WGC*RUTH CHRIS", True),
        ("Ruth's Chris", "Bistro", False),
        ("Ruth's Chris", "RUTH GARDENS", False),  # every word has to be there
        ("a b", "Bistro", False),  # one-letter words say nothing
        ("*", "WGC*RUTH CHRIS", True),  # only symbols: the plain text still counts
        ("*", "Bistro", False),
    ],
)
def test_search_finds_a_merchant_whatever_apostrophes_and_symbols_the_bank_used(
    needle: str, merchant: str, found: bool
) -> None:
    assert search_matches(needle, merchant) is found


def test_the_budget_search_finds_the_charge_by_the_name_people_say() -> None:
    svc = service()
    start = date(2026, 10, 1)
    result = svc.search(start, date(2026, 10, 31), merchant="Ruth's Chris")
    assert [row.txn_id for row in result.rows] == ["gift"]


@pytest.mark.parametrize(
    ("rule", "merchant", "applies"),
    [
        ("ruth's chris", "WGC*RUTH CHRIS", True),
        ("macy's", "MACY'S #123", True),
        ("macys", "MACY'S #123", True),
        ("macys", "MACY\u2019S #123", True),  # a curly apostrophe in the bank's own text
        ("o'reilly auto", "OREILLY AUTO PARTS", True),
        ("mart", "WALMART", False),  # still whole words only
        ("wgc*", "WGC*RUTH CHRIS", True),
        ("wgc*", "WGC GOLF", False),  # the star is part of the text
    ],
)
def test_a_rule_ignores_apostrophes_but_still_needs_whole_words(
    rule: str, merchant: str, applies: bool
) -> None:
    assert merchant_matches(normalize_match(rule), merchant) is applies


def test_the_ruths_chris_example_works_end_to_end_with_the_name_as_people_type_it() -> None:
    question = "The Ruth's Chris charge was really $100, someone paid us back $100"
    for typed in (question, question.replace("'", "\u2019")):
        svc = service()
        reply, llm = ask(
            svc,
            typed,
            [("search_transactions", {"merchant": "Ruth's Chris", "period": "this_month"})],
            [adjust(reimbursed_amount=100)],
        )
        assert reply.startswith("Adjusted WGC*RUTH CHRIS on 2026-10-03:"), typed
        assert spent(svc, Category.MISC) == Decimal("100.00")


def test_a_rule_can_be_made_for_a_name_with_an_apostrophe_and_it_moves_the_charge() -> None:
    svc = service()
    reply, _ = ask(
        svc,
        "Always count Ruth\u2019s Chris as restaurants",
        [("set_merchant_rule", {"merchant": "Ruth's Chris", "category": "restaurants"})],
    )
    assert reply.startswith('From now on, charges from "ruth\'s chris" count as')
    assert "moves 1 charge totalling $200.00" in reply
    assert spent(svc, Category.MISC) == Decimal("0.00")
