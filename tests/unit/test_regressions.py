"""Defects found by an independent review, each reproduced and fixed."""

from datetime import date, timedelta
from decimal import Decimal

import pytest

from budget_bot.categories import Category
from budget_bot.ledger_memory import MemoryLedger
from budget_bot.money import parse_amount
from budget_bot.ports import FeedStatus, SyncBatch
from budget_bot.render import pct_text
from budget_bot.report import build_report
from budget_bot.sync import run_sync
from budget_bot.transactions import find_duplicates, resolve_all
from scripts.import_csv import import_csv
from scripts.plaid_link import remove_item
from tests.helpers import CHASE, MORNING, ROBINHOOD, budget_service, txn
from tests.unit.test_csv_and_scripts import FakePlaid, Store, base_config

SUPERSTORE = {"primary": "GENERAL_MERCHANDISE", "detailed": "GENERAL_MERCHANDISE_SUPERSTORES"}
SHOPPING = {"primary": "GENERAL_MERCHANDISE", "detailed": "GENERAL_MERCHANDISE_ONLINE_MARKETPLACES"}


def committed(service, category: Category) -> Decimal:  # type: ignore[no-untyped-def]
    return service.month_summary().line(category).committed


# A correction follows its purchase when the record's identity changes


def test_correction_made_while_pending_survives_the_posted_replacement() -> None:
    service = budget_service(
        [txn("plaid:P", "80.00", date(2026, 10, 12), "Target", pending=True, **SUPERSTORE)]
    )
    service.recategorize("plaid:P", Category.GIFTS, "alice")
    # Plaid removes the pending record and adds a posted one that names it.
    service.ledger.upsert(
        [txn("plaid:Q", "80.00", date(2026, 10, 12), "Target", pending_of="plaid:P", **SUPERSTORE)]
    )
    service.ledger.remove(["plaid:P"])
    assert committed(service, Category.GIFTS) == Decimal("80.00")
    assert committed(service, Category.GROCERIES) == 0


def test_correction_survives_while_the_pending_twin_is_still_stored() -> None:
    service = budget_service(
        [txn("plaid:P", "80.00", date(2026, 10, 12), "Target", pending=True, **SUPERSTORE)]
    )
    service.recategorize("plaid:P", Category.GIFTS, "alice")
    service.ledger.upsert(
        [txn("plaid:Q", "80.00", date(2026, 10, 12), "Target", pending_of="plaid:P", **SUPERSTORE)]
    )
    assert committed(service, Category.GIFTS) == Decimal("80.00")
    assert committed(service, Category.GROCERIES) == 0


def test_correction_on_a_csv_row_carries_to_the_plaid_record_that_replaces_it() -> None:
    csv = txn("csv:abc", "40.00", date(2026, 10, 12), "TARGET 123", feed="csv:x", **SUPERSTORE)
    service = budget_service([csv])
    service.recategorize("csv:abc", Category.GIFTS, "alice")
    service.ledger.upsert(
        [txn("plaid:t", "40.00", date(2026, 10, 12), "Target", feed="plaid:item1", **SUPERSTORE)]
    )
    assert committed(service, Category.GIFTS) == Decimal("40.00")
    assert committed(service, Category.GROCERIES) == 0


def test_a_direct_correction_beats_an_inherited_one() -> None:
    pending = txn("plaid:P", "80.00", date(2026, 10, 12), "Target", pending=True, **SUPERSTORE)
    service = budget_service([pending])
    service.recategorize("plaid:P", Category.GIFTS, "alice")
    service.ledger.upsert(
        [txn("plaid:Q", "80.00", date(2026, 10, 12), "Target", pending_of="plaid:P", **SUPERSTORE)]
    )
    service.recategorize("plaid:Q", Category.PETS, "bob")
    assert (
        committed(service, Category.PETS) == Decimal("80.00")
        and committed(service, Category.GIFTS) == 0
    )


# Duplicate detection


def test_pending_in_one_feed_and_posted_in_another_count_once() -> None:
    pending = txn("plaid:p", "25.00", date(2026, 10, 13), "Cafe", pending=True, feed="plaid:item1")
    posted = txn(
        "csv:c", "25.00", date(2026, 10, 13), "CAFE", posted=date(2026, 10, 14), feed="csv:x"
    )
    assert find_duplicates([pending, posted]) == {"plaid:p": "superseded_by:csv:c"}
    assert committed(budget_service([pending, posted]), Category.MISC) == Decimal("25.00")


def test_dates_a_day_apart_between_feeds_are_the_same_purchase() -> None:
    plaid = txn(
        "plaid:a", "45.67", date(2026, 10, 12), posted=date(2026, 10, 13), feed="plaid:item1"
    )
    near = txn("csv:near", "45.67", date(2026, 10, 14), posted=date(2026, 10, 14), feed="csv:x")
    far = txn("csv:far", "45.67", date(2026, 10, 16), posted=date(2026, 10, 16), feed="csv:x")
    assert find_duplicates([plaid, near]) == {"csv:near": "duplicate_of:plaid:a"}
    assert find_duplicates([plaid, far]) == {}


def test_three_feeds_for_one_card_count_the_most_any_one_feed_saw() -> None:
    first = [txn("a1", "5.00", feed="plaid:item1")]
    second = [txn("b1", "5.00", feed="plaid:item2"), txn("b2", "5.00", feed="plaid:item2")]
    third = [txn("c1", "5.00", feed="csv:x"), txn("c2", "5.00", feed="csv:x")]
    counted = [r.txn.txn_id for r in resolve_all([*first, *second, *third], {}) if r.counted]
    assert len(counted) == 2


def test_a_pending_record_stranded_by_a_replaced_connection_is_not_counted_twice() -> None:
    # The old connection stopped syncing while the purchase was pending; the new one has it posted.
    ledger = MemoryLedger()
    ledger.upsert([txn("old:1", "50.00", pending=True, feed="plaid:old")])
    ledger.upsert([txn("new:1", "50.00", feed="plaid:new")])
    assert committed(budget_service(ledger=ledger), Category.MISC) == Decimal("50.00")


# What counts as spending


def test_a_card_payment_is_recognized_by_name_whatever_the_provider_called_it() -> None:
    payment = txn("pay", "-2500.00", merchant="AUTOMATIC PAYMENT - THANK YOU", primary="OTHER")
    service = budget_service(
        [payment, txn("g", "100.00", primary="FOOD_AND_DRINK", detailed="FOOD_AND_DRINK_GROCERIES")]
    )
    assert service.month_summary().committed == Decimal("100.00")


def test_a_positive_loan_payment_is_a_real_charge_but_a_negative_one_is_a_payment() -> None:
    charge = txn("c", "300.00", primary="LOAN_PAYMENTS", detailed="LOAN_PAYMENTS_OTHER_PAYMENT")
    credit = txn(
        "p", "-300.00", primary="LOAN_PAYMENTS", detailed="LOAN_PAYMENTS_CREDIT_CARD_PAYMENT"
    )
    results = {r.txn.txn_id: r for r in resolve_all([charge, credit], {})}
    assert results["c"].counted and results["c"].category == Category.MISC
    assert not results["p"].counted
    # Money arriving as income is not spending. A store return is handled separately.
    assert not resolve_all([txn("i", "10.00", primary="INCOME")], {})[0].counted


def test_costco_and_target_with_no_provider_category_still_count_as_groceries() -> None:
    results = {
        r.txn.merchant: r.category
        for r in resolve_all(
            [
                txn("a", merchant="TARGET 00012"),
                txn("b", merchant="COSTCO WHSE"),
                txn(
                    "c",
                    merchant="Costco Gas",
                    primary="TRANSPORTATION",
                    detailed="TRANSPORTATION_GAS",
                ),
            ],
            {},
        )
    }
    assert results["TARGET 00012"] == results["COSTCO WHSE"] == Category.GROCERIES
    assert results["Costco Gas"] == Category.TRANSPORTATION


# Undo walks back through corrections


def test_undo_restores_the_earlier_correction_then_the_original() -> None:
    service = budget_service([txn("a", "50.00", date(2026, 10, 6), "Amazon", **SHOPPING)])
    service.recategorize("a", Category.GIFTS, "u")
    service.recategorize("a", Category.PETS, "u")
    assert service.last_correction() == "a"
    assert service.undo_recategorization("a", "u") == Category.GIFTS
    assert committed(service, Category.GIFTS) == Decimal("50.00")
    assert service.last_correction() == "a"
    assert service.undo_recategorization("a", "u") == Category.SHOPPING
    assert service.ledger.overrides() == {} and service.last_correction() is None
    assert committed(service, Category.SHOPPING) == Decimal("50.00")


def test_last_correction_is_the_most_recent_one_still_in_force() -> None:
    service = budget_service(
        [
            txn("a", "50.00", date(2026, 10, 6), **SHOPPING),
            txn("b", "20.00", date(2026, 10, 7), **SHOPPING),
        ]
    )
    service.recategorize("a", Category.GIFTS, "u")
    service.recategorize("b", Category.PETS, "u")
    service.undo_recategorization("b", "u")
    assert service.last_correction() == "a"


def test_undo_works_after_the_record_was_replaced() -> None:
    service = budget_service(
        [txn("plaid:P", "80.00", date(2026, 10, 12), "Target", pending=True, **SUPERSTORE)]
    )
    service.recategorize("plaid:P", Category.GIFTS, "u")
    service.ledger.remove(["plaid:P"])
    assert service.undo_recategorization("plaid:P", "u") is None
    assert service.ledger.overrides() == {}


# Freshness is judged per card


def stamp(
    ledger: MemoryLedger,
    feed: str,
    accounts: tuple[str, ...],
    age_days: int = 0,
    error: str | None = None,
) -> None:
    when = (MORNING - timedelta(days=age_days)).isoformat()
    ledger.save_status(FeedStatus(feed, when, error, accounts))


def test_an_old_backfill_csv_does_not_warn_while_plaid_is_fresh() -> None:
    ledger = MemoryLedger()
    stamp(ledger, "csv:chase-sapphire-preferred", (CHASE,), age_days=40)
    stamp(ledger, "plaid:item1", (CHASE, ROBINHOOD))
    assert budget_service(ledger=ledger, synced=False).coverage().problems == ()


def test_a_replaced_connection_with_an_old_error_does_not_warn_when_the_new_one_is_fine() -> None:
    ledger = MemoryLedger()
    stamp(ledger, "plaid:old", (CHASE,), age_days=3, error="ITEM_LOGIN_REQUIRED")
    stamp(ledger, "plaid:new", (CHASE, ROBINHOOD))
    assert budget_service(ledger=ledger, synced=False).coverage().problems == ()


def test_a_card_whose_only_connection_failed_is_still_reported() -> None:
    ledger = MemoryLedger()
    stamp(ledger, "plaid:old", (CHASE,), error="ITEM_LOGIN_REQUIRED")
    stamp(ledger, "plaid:rh", (ROBINHOOD,))
    assert budget_service(ledger=ledger, synced=False).coverage().problems == (
        "Chase Sapphire Preferred needs to be re-linked",
    )


def test_a_card_whose_only_connection_went_stale_is_still_reported() -> None:
    ledger = MemoryLedger()
    stamp(ledger, "plaid:chase", (CHASE,), age_days=3)
    stamp(ledger, "plaid:rh", (ROBINHOOD,))
    (problem,) = budget_service(ledger=ledger, synced=False).coverage().problems
    assert problem == "Chase Sapphire Preferred has not refreshed since Oct 11"


def test_a_csv_import_is_current_only_as_far_as_the_file_goes() -> None:
    text = (
        "Transaction Date,Post Date,Description,Category,Type,Amount,Memo\n"
        "08/30/2026,08/31/2026,OLD SHOP,Shopping,Sale,-10.00,\n"
    )
    from budget_bot.csv_import import CHASE as CHASE_FORMAT

    ledger = MemoryLedger()
    import_csv(text, CHASE, CHASE_FORMAT, ledger, MORNING)  # type: ignore[arg-type]
    (status,) = ledger.statuses()
    # 11:59:59 PM Central on 31 August is 04:59:59 UTC on 1 September.
    assert status.last_success == "2026-09-01T04:59:59+00:00"
    assert budget_service(ledger=ledger, synced=False).coverage().feeds[0].stale


def test_removing_a_connection_can_clear_its_stored_transactions() -> None:
    config = base_config(
        items=[{"item_id": "item-1", "access_token": "access-x", "accounts": {"a": CHASE}}]
    )
    store, plaid, purged = Store(config), FakePlaid(), []
    message = remove_item(store, plaid, "item-1", lambda feed: purged.append(feed) or 7)  # type: ignore[arg-type]
    assert purged == ["plaid:item-1"] and "Deleted 7" in message
    assert plaid.removed == ["access-x"] and store.value["items"] == []  # type: ignore[index]
    again = Store(config)
    assert "ledger_admin.py purge --feed plaid:item-1" in remove_item(again, FakePlaid(), "item-1")  # type: ignore[arg-type]
    with pytest.raises(ValueError):
        remove_item(Store(config), FakePlaid(), "missing")  # type: ignore[arg-type]


# A purchase near a month end keeps its month


def test_a_posted_record_keeps_the_pending_records_earlier_date() -> None:
    ledger = MemoryLedger()
    ledger.upsert([txn("plaid:P", "120.00", date(2026, 10, 31), pending=True)])
    posted = txn(
        "plaid:Q", "120.00", date(2026, 11, 1), pending_of="plaid:P", posted=date(2026, 11, 1)
    )

    class Provider:
        def feeds(self) -> list[str]:
            return ["plaid:item1"]

        def fetch(self, feed: str, cursor: str | None) -> SyncBatch:
            return SyncBatch((posted,), ("plaid:P",), "c1", (CHASE,))

    run_sync(Provider(), ledger, ledger, MORNING)
    assert ledger.get("plaid:Q").spend_date == date(2026, 10, 31)  # type: ignore[union-attr]
    assert ledger.get("plaid:P") is None


# Wording and parsing


@pytest.mark.parametrize(
    ("part", "whole", "text"),
    [
        ("1492.60", "1500", "99%"),
        ("1507", "1500", "100%"),
        ("1500", "1500", "100%"),
        ("750", "1500", "50%"),
    ],
)
def test_a_percentage_never_claims_a_limit_is_used_while_room_remains(
    part: str, whole: str, text: str
) -> None:
    assert pct_text(Decimal(part), Decimal(whole)) == text


def test_going_over_the_whole_budget_is_said_plainly() -> None:
    service = budget_service([txn("big", "6500.00", date(2026, 10, 5), **SHOPPING)])
    from budget_bot.render import totals_text

    text = totals_text(service.month_summary())
    assert "$220.00 over budget" in text and "-$" not in text
    assert "left" not in text.split("spent,")[1].split(",")[0]


def test_a_day_with_only_refunds_does_not_name_a_largest_purchase() -> None:
    refund = txn("r", "-25.99", date(2026, 10, 12), "Shop", posted=date(2026, 10, 13), **SHOPPING)
    text = build_report(budget_service([refund]))
    assert "Yesterday: $25.99 refunded." in text
    assert "largest" not in text


@pytest.mark.parametrize("text", ["12,50", "1,00", "1_000", "1,2345", "--5", "1.2.3", "$", ""])
def test_ambiguous_amounts_are_rejected(text: str) -> None:
    with pytest.raises(ValueError):
        parse_amount(text)


@pytest.mark.parametrize(
    ("text", "expected"),
    [
        ("1,234.50", "1234.50"),
        ("$1,000", "1000.00"),
        ("(1,234.00)", "-1234.00"),
        ("+5", "5.00"),
        ("1234567.1", "1234567.10"),
    ],
)
def test_unambiguous_amounts_still_parse(text: str, expected: str) -> None:
    assert parse_amount(text) == Decimal(expected)


# Security review


def test_a_run_waits_for_the_lock_then_gives_up_when_it_stays_busy() -> None:
    ledger = MemoryLedger()
    ledger.lock("sync-plaid:a", "someone-else", 600)

    class Provider:
        def feeds(self) -> list[str]:
            return ["plaid:a"]

        def fetch(self, feed: str, cursor: str | None) -> SyncBatch:
            return SyncBatch((), (), "c", (CHASE,))

    slept: list[float] = []
    report = run_sync(Provider(), ledger, ledger, MORNING, "plaid:a", 9.0, slept.append)
    assert report.outcomes == {"plaid:a": "busy"} and slept == [3.0, 3.0, 3.0]
    # Without a wait allowance it does not sleep at all.
    slept.clear()
    run_sync(Provider(), ledger, ledger, MORNING, "plaid:a", 0.0, slept.append)
    assert slept == []


@pytest.mark.parametrize(
    ("status", "body", "retryable"),
    [
        (400, b'{"error_type":"ITEM_ERROR","error_code":"INSTITUTION_DOWN"}', True),
        (400, b'{"error_type":"ITEM_ERROR","error_code":"PRODUCT_NOT_READY"}', True),
        (400, b'{"error_type":"ITEM_ERROR","error_code":"INSTITUTION_NOT_RESPONDING"}', True),
        (400, b'{"error_type":"RATE_LIMIT_EXCEEDED","error_code":"TRANSACTIONS_LIMIT"}', True),
        (400, b'{"error_type":"API_ERROR","error_code":"PLANNED_MAINTENANCE"}', True),
        (400, b'{"error_type":"ITEM_ERROR","error_code":"ITEM_LOGIN_REQUIRED"}', False),
        (400, b'{"error_type":"INVALID_REQUEST","error_code":"INVALID_FIELD"}', False),
    ],
)
def test_temporary_plaid_problems_are_retried_even_when_reported_as_4xx(
    status: int, body: bytes, retryable: bool
) -> None:
    from budget_bot.plaid import PlaidClient, PlaidError
    from tests.unit.test_plaid import Opener, http_error

    client = PlaidClient("sandbox", "a", "b", Opener(http_error(status, body)))
    with pytest.raises(PlaidError) as caught:
        client.call("/x", {})
    assert caught.value.retryable is retryable


def test_a_question_and_its_token_never_appear_in_a_repr() -> None:
    from budget_bot.discord import Command
    from budget_bot.models import Question

    question = Question("300", "100000000000000004", "how much at Costco?", "SECRET-TOKEN")
    command = Command("1", "2", "3", "4", "5", "ask", "how much at Costco?", "SECRET-TOKEN")
    for shown in (repr(question), repr(command), str(question)):
        assert "SECRET-TOKEN" not in shown and "Costco" not in shown


def test_merchant_text_cannot_form_a_link_a_code_span_or_a_spoiler() -> None:
    from budget_bot.transactions import clean_text

    hostile = "ZELLE [Chase](https://evil.example) <https://evil.example> `x` ||spoiler||"
    cleaned = clean_text(hostile, 200)
    assert not any(char in cleaned for char in "[]<>`|")
    assert "](" not in cleaned
    # Ordinary merchant punctuation survives.
    assert clean_text("AMAZON.COM*AB12 #1 (PRIME)") == "AMAZON.COM*AB12 #1 (PRIME)"


def test_messages_ask_discord_not_to_unfurl_links() -> None:
    from budget_bot.discord import SUPPRESS_EMBEDS, _message, private_message

    assert _message("hi")["flags"] == SUPPRESS_EMBEDS == 4
    # A private (ephemeral) refusal keeps its own flag.
    assert private_message("no")["data"]["flags"] == 64


@pytest.mark.parametrize("name", ["*", "household-budget/*", "a b", "x" * 300, "", "a?b"])
def test_secret_names_cannot_widen_an_iam_grant(name: str, monkeypatch: pytest.MonkeyPatch) -> None:
    from budget_bot.config import Settings

    monkeypatch.setenv("PLAID_SECRET_NAME", name)
    with pytest.raises(ValueError):
        Settings.from_env()


def test_the_csv_script_refuses_a_file_whose_amount_signs_look_reversed() -> None:
    from budget_bot.csv_import import CsvFormat

    text = "Date,Amount,Description\n2026-10-01,45.10,WHOLE FOODS\n2026-10-02,12.00,COFFEE\n"
    reversed_signs = CsvFormat("Date", "Amount", "Description", spend_is_negative=True)
    right_signs = CsvFormat("Date", "Amount", "Description", spend_is_negative=False)
    ledger = MemoryLedger()
    with pytest.raises(ValueError, match="reversed"):
        import_csv(text, CHASE, reversed_signs, ledger, MORNING)  # type: ignore[arg-type]
    assert ledger.transactions == {}
    summary = import_csv(text, CHASE, right_signs, ledger, MORNING)  # type: ignore[arg-type]
    assert summary.spending == Decimal("57.10") and len(ledger.transactions) == 2
    # A dry run still reports the negative total so the sign problem is visible before --apply.
    assert import_csv(text, CHASE, reversed_signs, None, MORNING).spending == Decimal("-57.10")  # type: ignore[arg-type]


def test_linking_removes_the_connection_if_anything_fails_after_the_exchange() -> None:
    from scripts.plaid_link import add_item
    from tests.unit.test_csv_and_scripts import ACCOUNTS as PLAID_ACCOUNTS

    class FailingStore(Store):
        def save(self, value: dict) -> None:  # type: ignore[type-arg]
            raise RuntimeError("secrets manager is unavailable")

    plaid = FakePlaid(accounts=PLAID_ACCOUNTS)
    with pytest.raises(RuntimeError):
        add_item(FailingStore(base_config()), plaid, "p", lambda _p: "1", lambda _l: None)  # type: ignore[arg-type]
    assert plaid.removed == ["access-sandbox-xyz"]

    interrupted = FakePlaid(accounts=PLAID_ACCOUNTS)

    def abort(_prompt: str) -> str:
        raise KeyboardInterrupt

    with pytest.raises(KeyboardInterrupt):
        add_item(Store(base_config()), interrupted, "p", abort, lambda _l: None)  # type: ignore[arg-type]
    assert interrupted.removed == ["access-sandbox-xyz"]


def test_removing_an_item_plaid_no_longer_knows_still_clears_the_secret() -> None:
    from budget_bot.plaid import PlaidError

    class GoneAtPlaid(FakePlaid):
        def item_remove(self, access_token: str) -> dict:  # type: ignore[type-arg]
            raise PlaidError("ITEM_NOT_FOUND", retryable=False)

    config = base_config(items=[{"item_id": "i", "access_token": "access-x", "accounts": {}}])
    store = Store(config)
    assert "Removed" in remove_item(store, GoneAtPlaid(), "i")  # type: ignore[arg-type]
    assert store.value["items"] == []  # type: ignore[index]
    broken = Store(config)

    class Down(FakePlaid):
        def item_remove(self, access_token: str) -> dict:  # type: ignore[type-arg]
            raise PlaidError("NETWORK_ERROR")

    with pytest.raises(PlaidError):
        remove_item(broken, Down(), "i")  # type: ignore[arg-type]
    assert broken.saves == 0
