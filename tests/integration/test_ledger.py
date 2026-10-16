from collections.abc import Callable, Iterator
from datetime import UTC, date, datetime, timedelta
from decimal import Decimal
from typing import Any

import boto3
import pytest
from moto import mock_aws

from budget_bot.budget import BudgetService, LimitChange
from budget_bot.categories import Category
from budget_bot.changes import (
    adjust_charge,
    change_defaults,
    remove_rule,
    set_rule,
    undo_adjustment,
    undo_defaults,
    undo_rule_change,
)
from budget_bot.fixtures import FixtureProvider, fixture_transactions
from budget_bot.ledger import INDEX, RETENTION_DAYS, DynamoLedger
from budget_bot.ledger_memory import MemoryLedger
from budget_bot.ports import BudgetConflict, BudgetRecord, FeedStatus
from budget_bot.state import DynamoDailyLimit
from budget_bot.sync import run_sync
from budget_bot.transactions import AuditEntry, Override
from tests.helpers import CHASE, MORNING, NOW, txn

pytestmark = pytest.mark.integration


def create_ledger_table(resource: Any) -> Any:
    def key(name: str) -> dict[str, str]:
        return {"AttributeName": name, "AttributeType": "S"}

    return resource.create_table(
        TableName="ledger",
        KeySchema=[
            {"AttributeName": "pk", "KeyType": "HASH"},
            {"AttributeName": "sk", "KeyType": "RANGE"},
        ],
        AttributeDefinitions=[key("pk"), key("sk"), key("gsi1pk"), key("gsi1sk")],
        GlobalSecondaryIndexes=[
            {
                "IndexName": INDEX,
                "KeySchema": [
                    {"AttributeName": "gsi1pk", "KeyType": "HASH"},
                    {"AttributeName": "gsi1sk", "KeyType": "RANGE"},
                ],
                "Projection": {"ProjectionType": "ALL"},
            }
        ],
        BillingMode="PAY_PER_REQUEST",
    )


@pytest.fixture
def table() -> Iterator[Any]:
    with mock_aws():
        yield create_ledger_table(boto3.resource("dynamodb", region_name="us-east-2"))


@pytest.fixture
def ledger(table: Any) -> DynamoLedger:
    return DynamoLedger(table, clock=lambda: NOW)


def test_transactions_round_trip_exactly(ledger: DynamoLedger) -> None:
    original = txn(
        "plaid:abc",
        "-12.34",
        date(2026, 10, 5),
        "Cafe",
        primary="FOOD_AND_DRINK",
        detailed="FOOD_AND_DRINK_COFFEE",
        pending_of="plaid:old",
    )
    pending = txn("plaid:p", "5.00", date(2026, 10, 6), pending=True)
    ledger.upsert([original, pending])
    assert ledger.get("plaid:abc") == original and ledger.get("plaid:p") == pending
    assert ledger.get("missing") is None
    assert isinstance(ledger.get("plaid:abc").amount, Decimal)  # type: ignore[union-attr]


def test_transactions_expire_after_the_retention_period(table: Any, ledger: DynamoLedger) -> None:
    ledger.upsert([txn("a", "1.00", date(2026, 10, 5))])
    item = table.get_item(Key={"pk": "TXN#a", "sk": "TXN"})["Item"]
    expected = datetime(2026, 10, 5, tzinfo=UTC) + timedelta(days=RETENTION_DAYS)
    assert item["ttl"] == int(expected.timestamp())
    # Corrections and the audit trail are tiny and kept; only transactions expire.
    ledger.set_override(
        Override("a", Category.GIFTS, "u", "t"), AuditEntry("t", "u", "recategorize", "a", "x", "y")
    )
    assert "ttl" not in table.get_item(Key={"pk": "OVERRIDE", "sk": "a"})["Item"]


BUDGET_AUDIT = AuditEntry(
    "2026-10-14T13:00:00+00:00", "u", "budget_change", "budget:2026-10", "{}", "{}"
)


def test_a_months_budget_record_round_trips_and_is_never_deleted(
    table: Any, ledger: DynamoLedger
) -> None:
    assert ledger.budget_record("2026-10") == BudgetRecord()
    history = [
        {
            "by": "u",
            "at": "2026-10-14T13:00:00+00:00",
            "from": {"restaurants": "750"},
            "to": {"restaurants": "900.00"},
        }
    ]
    ledger.save_budget_record("2026-10", {"restaurants": "900.00"}, history, 0, BUDGET_AUDIT)
    record = ledger.budget_record("2026-10")
    assert record.limits == {"restaurants": "900.00"} and record.revision == 1
    assert record.history == tuple(history)
    assert ledger.budget_record("2026-11") == BudgetRecord()  # each month is its own record
    # A save replaces. Going back to all defaults keeps the (now empty) record, so a stale writer
    # can never recreate one, and the audit trail keeps every save.
    ledger.save_budget_record("2026-10", {}, [], 1, BUDGET_AUDIT)
    item = table.get_item(Key={"pk": "BUDGET", "sk": "2026-10"})["Item"]
    assert item["limits"] == {} and item["history"] == [] and item["rev"] == 2
    assert "ttl" not in item  # tiny and kept: it never expires like transactions do
    assert [entry.action for entry in ledger.audit_log(10)] == ["budget_change"] * 2


def test_a_stale_budget_save_is_refused_and_leaves_no_trace(ledger: DynamoLedger) -> None:
    ledger.save_budget_record("2026-10", {"travel": "300"}, [], 0, BUDGET_AUDIT)
    with pytest.raises(BudgetConflict):  # written by someone who read before that first save
        ledger.save_budget_record("2026-10", {"travel": "999"}, [], 0, BUDGET_AUDIT)
    ledger.save_budget_record("2026-10", {"travel": "400"}, [], 1, BUDGET_AUDIT)
    with pytest.raises(BudgetConflict):  # and again with a revision that is now out of date
        ledger.save_budget_record("2026-10", {"travel": "999"}, [], 1, BUDGET_AUDIT)
    assert ledger.budget_record("2026-10").limits == {"travel": "400"}
    assert len(ledger.audit_log(10)) == 2  # only the two saves that happened were logged


def test_a_failed_audit_line_does_not_turn_a_saved_change_into_an_error(
    ledger: DynamoLedger, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    def refuse(_audit: AuditEntry) -> None:
        raise RuntimeError("throttled")

    monkeypatch.setattr(ledger, "_write_audit", refuse)
    ledger.save_budget_record("2026-10", {"travel": "300"}, [{"by": "u"}], 0, BUDGET_AUDIT)
    assert ledger.budget_record("2026-10").limits == {"travel": "300"}
    assert "setting_audit_write_failed" in caplog.text


class RacingDynamo(DynamoLedger):
    """A ledger where a rival writer gets in between this writer's read and its save, once."""

    rival: Callable[[], None] | None = None

    def save_budget_record(self, *args: Any, **kwargs: Any) -> None:
        if self.rival is not None:
            rival, self.rival = self.rival, None
            rival()
        super().save_budget_record(*args, **kwargs)


def test_two_people_changing_the_budget_at_once_keep_both_changes_in_dynamodb(
    table: Any,
) -> None:
    ledger = RacingDynamo(table, clock=lambda: NOW)
    ours = BudgetService(ledger, ledger, lambda: MORNING)
    theirs = BudgetService(ledger, ledger, lambda: MORNING)
    ledger.rival = lambda: theirs.change_limits(
        [LimitChange(Category.SHOPPING, change_by=Decimal("100"))], "bob"
    )
    ours.change_limits([LimitChange(Category.DINING, change_by=Decimal("100"))], "alice")
    limits = ours.limits(2026, 10)
    assert limits[Category.DINING] == Decimal("850.00")
    assert limits[Category.SHOPPING] == Decimal("1600.00")  # the rival's change survived
    assert [entry["by"] for entry in ledger.budget_record("2026-10").history] == ["bob", "alice"]


def test_the_budget_can_be_changed_and_undone_through_dynamodb(ledger: DynamoLedger) -> None:
    # The clock never moves, so every audit entry shares one timestamp. Undo does not depend on
    # their order: the history it walks back through lives on the month's own record.
    service = BudgetService(ledger, ledger, lambda: MORNING)
    ledger.upsert([txn("t1", "40.00", date(2026, 10, 5), primary="FOOD_AND_DRINK")])
    service.change_limits([LimitChange(Category.DINING, set_to=Decimal("900"))], "u1")
    assert service.month_summary().budget == Decimal("6430.00")
    assert service.limits(2026, 10)[Category.DINING] == Decimal("900.00")
    assert service.limits(2026, 11)[Category.DINING] == Decimal("750")
    service.change_limits([LimitChange(Category.SHOPPING, change_by=Decimal("-100"))], "u2")
    service.undo_limit_change("u1")  # undoes the latest change (shopping), not the first
    assert service.limits(2026, 10)[Category.SHOPPING] == Decimal("1500")
    assert service.limits(2026, 10)[Category.DINING] == Decimal("900.00")
    service.undo_limit_change("u1")
    assert service.month_summary().budget == Decimal("6280")
    assert ledger.budget_record("2026-10").limits == {}
    assert ledger.budget_record("2026-10").revision == 4


def test_household_settings_round_trip_and_only_known_kinds_are_allowed(
    table: Any, ledger: DynamoLedger
) -> None:
    audit = AuditEntry("2026-10-14T13:00:00+00:00", "u", "adjust", "gift", "", "100.00")
    nested = {
        "counted": "100.00",
        "history": [{"by": "u", "from": "", "to": "100.00", "note": "x"}],
    }
    assert ledger.setting("ADJUST", "gift").revision == 0 and ledger.settings_of("ADJUST") == {}
    ledger.save_setting("ADJUST", "gift", nested, 0, audit)
    ledger.save_setting("ADJUST", "other", {"counted": "5.00"}, 0, audit)
    ledger.save_setting(
        "RULES", "ALL", {"rules": [{"match": "wgc*", "category": "pets"}]}, 0, audit
    )
    record = ledger.setting("ADJUST", "gift")
    assert record.data == nested and record.revision == 1
    assert set(ledger.settings_of("ADJUST")) == {"gift", "other"}
    assert ledger.setting("RULES", "ALL").data["rules"][0]["match"] == "wgc*"
    assert "ttl" not in table.get_item(Key={"pk": "ADJUST", "sk": "gift"})["Item"]
    for bad in ("TXN#x", "OVERRIDE", "CURSOR", "", "adjust"):  # nothing outside the three kinds
        with pytest.raises(ValueError, match="unknown setting kind"):
            ledger.save_setting(bad, "k", {}, 0, audit)
        with pytest.raises(ValueError, match="unknown setting kind"):
            ledger.setting(bad, "k")
        with pytest.raises(ValueError, match="unknown setting kind"):
            ledger.settings_of(bad)


def test_a_stale_setting_save_is_refused_and_leaves_no_trace(ledger: DynamoLedger) -> None:
    audit = AuditEntry("2026-10-14T13:00:00+00:00", "u", "rule_set", "rule:wgc*", "", "pets")
    ledger.save_setting("RULES", "ALL", {"rules": []}, 0, audit)
    with pytest.raises(BudgetConflict):
        ledger.save_setting("RULES", "ALL", {"rules": ["stale"]}, 0, audit)
    ledger.save_setting("RULES", "ALL", {"rules": ["fresh"]}, 1, audit)
    with pytest.raises(BudgetConflict):
        ledger.save_setting("RULES", "ALL", {"rules": ["stale"]}, 1, audit)
    assert ledger.setting("RULES", "ALL").data == {"rules": ["fresh"]}
    assert len(ledger.audit_log(10)) == 2


def household(ledger: DynamoLedger) -> BudgetService:
    """A service over the real table logic, with a clock that moves as the real one does."""
    moment = [MORNING]

    def clock() -> datetime:
        moment[0] += timedelta(seconds=1)
        return moment[0]

    return BudgetService(ledger, ledger, clock)


GIFT = txn(
    "gift",
    "200.00",
    date(2026, 10, 3),
    "WGC*RUTH CHRIS",
    primary="TRANSFER_OUT",
    detailed="TRANSFER_OUT_ACCOUNT_TRANSFER",
)


def test_charge_adjustments_work_end_to_end_through_dynamodb(ledger: DynamoLedger) -> None:
    ledger.upsert(
        [GIFT, txn("bistro", "80.00", date(2026, 10, 9), "Bistro", primary="FOOD_AND_DRINK")]
    )
    service = household(ledger)
    assert service.month_summary().line(Category.MISC).committed == Decimal("200.00")
    adjust_charge(service, "gift", "u1", reimbursed=Decimal("100"), note="gift")
    adjust_charge(service, "bistro", "u1", counted=Decimal("30"))
    summary = service.month_summary()
    assert summary.line(Category.MISC).committed == Decimal("100.00")
    assert (summary.adjusted_count, summary.adjusted_total) == (2, Decimal("150.00"))
    first = undo_adjustment(service, "u1")  # the newest: the bistro
    assert first.txn_id == "bistro" and first.after is None
    second = undo_adjustment(service, "u1")
    assert second.txn_id == "gift" and service.month_summary().adjusted_count == 0
    assert ledger.setting("ADJUST", "gift").data["counted"] == ""  # kept, not deleted


def test_two_people_adjusting_the_same_charge_at_once_do_not_lose_a_change(table: Any) -> None:
    class Racing(DynamoLedger):
        rival: Callable[[], None] | None = None

        def save_setting(self, *args: Any, **kwargs: Any) -> None:
            if self.rival is not None:
                rival, self.rival = self.rival, None
                rival()
            super().save_setting(*args, **kwargs)

    ledger = Racing(table, clock=lambda: NOW)
    ledger.upsert([GIFT])
    ours, theirs = household(ledger), household(ledger)
    ledger.rival = lambda: adjust_charge(theirs, "gift", "bob", counted=Decimal("150"), note="b")
    adjust_charge(ours, "gift", "alice", counted=Decimal("100"), note="a")
    record = ledger.setting("ADJUST", "gift")
    assert record.data["counted"] == "100.00"  # ours, applied after theirs
    assert [entry["by"] for entry in record.data["history"]] == ["bob", "alice"]
    assert record.data["history"][1]["from"] == "150.00"  # it saw their change before replacing it


def test_merchant_rules_and_standing_limits_work_end_to_end_through_dynamodb(
    ledger: DynamoLedger,
) -> None:
    ledger.upsert([GIFT])
    service = household(ledger)
    rule = set_rule(service, "WGC*", Category.GIFTS, "u1")
    assert (rule.affected_count, rule.affected_total) == (1, Decimal("200.00"))
    assert service.month_summary().line(Category.GIFTS).committed == Decimal("200.00")
    change_defaults(service, [LimitChange(Category.GIFTS, set_to=Decimal("250"))], "u1")
    assert service.limits(2026, 11)[Category.GIFTS] == Decimal("250.00")
    assert service.limits(2026, 9)[Category.GIFTS] == Decimal("55")
    undo_defaults(service, "u1")
    assert service.limits(2026, 11)[Category.GIFTS] == Decimal("55")
    remove_rule(service, "wgc*", "u1")
    undo_rule_change(service, "u1")  # puts the rule back
    assert service.month_summary().line(Category.GIFTS).committed == Decimal("200.00")
    undo_rule_change(service, "u1")  # and the original rule is undone too
    assert service.month_summary().line(Category.MISC).committed == Decimal("200.00")


def test_between_returns_only_the_requested_days_across_months(ledger: DynamoLedger) -> None:
    days = [
        date(2026, 9, 29),
        date(2026, 9, 30),
        date(2026, 10, 1),
        date(2026, 10, 31),
        date(2026, 11, 1),
    ]
    ledger.upsert([txn(f"t{i}", "1.00", day) for i, day in enumerate(days)])
    found = ledger.between(date(2026, 9, 30), date(2026, 10, 31))
    assert sorted(t.txn_id for t in found) == ["t1", "t2", "t3"]
    assert [t.txn_id for t in ledger.between(date(2026, 11, 1), date(2026, 11, 1))] == ["t4"]
    assert ledger.between(date(2027, 1, 1), date(2027, 1, 31)) == []


def test_across_a_year_boundary(ledger: DynamoLedger) -> None:
    ledger.upsert([txn("a", "1.00", date(2025, 12, 31)), txn("b", "1.00", date(2026, 1, 1))])
    assert len(ledger.between(date(2025, 12, 1), date(2026, 1, 31))) == 2


def test_upsert_is_idempotent_and_a_changed_date_moves_the_record(ledger: DynamoLedger) -> None:
    ledger.upsert([txn("a", "10.00", date(2026, 9, 30))])
    ledger.upsert([txn("a", "10.00", date(2026, 9, 30))])
    ledger.upsert([txn("a", "10.00", date(2026, 10, 1))])
    assert ledger.between(date(2026, 9, 1), date(2026, 9, 30)) == []
    assert [t.txn_id for t in ledger.between(date(2026, 10, 1), date(2026, 10, 31))] == ["a"]


def test_remove_deletes_and_ignores_unknown_ids(ledger: DynamoLedger) -> None:
    ledger.upsert([txn("a"), txn("b")])
    ledger.remove(["a", "never-stored"])
    assert ledger.get("a") is None and ledger.get("b") is not None


def test_queries_follow_pagination(table: Any) -> None:
    class SmallPages:
        """Forces DynamoDB to return two items per page."""

        def __init__(self, inner: Any) -> None:
            self.inner = inner

        def query(self, **kwargs: Any) -> Any:
            return self.inner.query(Limit=2, **kwargs)

        def __getattr__(self, name: str) -> Any:
            return getattr(self.inner, name)

    ledger = DynamoLedger(SmallPages(table))
    ledger.upsert([txn(f"t{i}", "1.00", date(2026, 10, 1 + i)) for i in range(7)])
    assert len(ledger.between(date(2026, 10, 1), date(2026, 10, 31))) == 7


def test_overrides_and_audit_trail(ledger: DynamoLedger) -> None:
    ledger.upsert([txn("a")])
    first = AuditEntry(
        "2026-10-14T13:00:00+00:00", "u1", "recategorize", "a", "shopping", "gifts_donations"
    )
    ledger.set_override(Override("a", Category.GIFTS, "u1", first.at), first)
    stored = ledger.overrides()["a"]
    assert (stored.txn_id, stored.category, stored.revision) == ("a", Category.GIFTS, 1)
    assert stored.history == ("gifts_donations",)
    undo = AuditEntry("2026-10-14T14:00:00+00:00", "u2", "undo", "a", "gifts_donations", "shopping")
    ledger.clear_override("a", undo)
    assert ledger.overrides() == {}
    assert ledger.audit_log(10) == [undo, first]  # newest first
    assert ledger.audit_log(1) == [undo]


def test_removing_a_transaction_leaves_its_correction_inert(ledger: DynamoLedger) -> None:
    ledger.upsert([txn("a")])
    ledger.set_override(
        Override("a", Category.GIFTS, "u", "t"), AuditEntry("t", "u", "recategorize", "a", "x", "y")
    )
    ledger.remove(["a"])
    assert "a" in ledger.overrides() and ledger.get("a") is None


def test_cursors_and_statuses(ledger: DynamoLedger) -> None:
    assert ledger.cursor("plaid:a") is None
    ledger.save_cursor("plaid:a", "cursor-1")
    ledger.save_cursor("plaid:a", "cursor-2")
    assert ledger.cursor("plaid:a") == "cursor-2" and ledger.cursor("plaid:b") is None
    ok = FeedStatus("plaid:a", "2026-10-14T13:00:00+00:00", None, (CHASE,))
    broken = FeedStatus("plaid:b", None, "ITEM_LOGIN_REQUIRED", ())
    ledger.save_status(ok)
    ledger.save_status(broken)
    assert sorted(ledger.statuses(), key=lambda s: s.feed) == [ok, broken]


def test_locks_are_exclusive_expire_and_belong_to_their_owner(table: Any) -> None:
    clock = [1000.0]
    ledger = DynamoLedger(table, clock=lambda: clock[0])
    assert ledger.lock("sync-a", "one", 120)
    assert not ledger.lock("sync-a", "two", 120)
    ledger.unlock("sync-a", "two")  # not the owner: no effect
    assert not ledger.lock("sync-a", "two", 120)
    ledger.unlock("sync-a", "one")
    assert ledger.lock("sync-a", "two", 120)
    clock[0] += 121
    assert ledger.lock("sync-a", "three", 120)  # the lease expired
    ledger.unlock("sync-b", "anyone")  # unknown lock: no error


def test_dynamo_and_memory_ledgers_give_identical_budget_answers(ledger: DynamoLedger) -> None:
    """The same synthetic month, through both stores, must produce the same numbers."""
    today = date(2026, 10, 14)
    memory = MemoryLedger()
    for store in (ledger, memory):
        run_sync(FixtureProvider(today), store, store, MORNING)  # type: ignore[arg-type]
    assert len(ledger.between(date(2026, 8, 1), today)) == len(fixture_transactions(today))
    expected = BudgetService(memory, memory, lambda: MORNING)
    actual = BudgetService(ledger, ledger, lambda: MORNING)
    for service in (expected, actual):
        # Order differs between stores, so choose the transaction by ID, not by position.
        october = service.ledger.between(today.replace(day=1), today)
        service.recategorize(min(october, key=lambda t: t.txn_id).txn_id, Category.GIFTS, "u")
    assert actual.month_summary() == expected.month_summary()
    assert actual.month_summary(2026, 9) == expected.month_summary(2026, 9)
    assert actual.coverage() == expected.coverage()
    assert actual.explain_change(Category.DINING) == expected.explain_change(Category.DINING)


def test_daily_limit_counts_per_day_and_stops_at_the_limit() -> None:
    with mock_aws():
        resource = boto3.resource("dynamodb", region_name="us-east-2")
        state = resource.create_table(
            TableName="messaging-state",
            KeySchema=[{"AttributeName": "pk", "KeyType": "HASH"}],
            AttributeDefinitions=[{"AttributeName": "pk", "AttributeType": "S"}],
            BillingMode="PAY_PER_REQUEST",
        )
        limit = DynamoDailyLimit(state, clock=lambda: NOW)
        assert [limit.take("2026-10-14", 3) for _ in range(5)] == [True, True, True, False, False]
        assert limit.take("2026-10-15", 3)
        item = state.get_item(Key={"pk": "AILIMIT#2026-10-14"})["Item"]
        assert item["uses"] == 3 and item["ttl"] > NOW


def test_utc_now_is_the_recorded_freshness_time(ledger: DynamoLedger) -> None:
    ledger.save_status(
        FeedStatus("f", datetime(2026, 10, 14, 13, 0, tzinfo=UTC).isoformat(), None, ())
    )
    assert ledger.statuses()[0].last_success == "2026-10-14T13:00:00+00:00"
