from datetime import date, datetime
from decimal import Decimal
from typing import Any

import pytest

from budget_bot.budget import HOUSEHOLD_TIME, BudgetService
from budget_bot.categories import Category
from budget_bot.credentials import PlaidConfig, PlaidItem, parse_plaid_config, plaid_config
from budget_bot.fixtures import FixtureProvider
from budget_bot.ledger_memory import MemoryLedger
from budget_bot.plaid import PlaidError, PlaidProvider
from budget_bot.ports import FeedStatus, ProviderError, SyncBatch
from budget_bot.report import build_report
from budget_bot.sync import run_sync
from tests.helpers import CHASE, MORNING, txn
from tests.unit.test_plaid import ACCOUNTS, FakeClient, page, raw


class StubProvider:
    def __init__(self, outcomes: dict[str, Any]) -> None:
        self.outcomes, self.fetched = outcomes, []

    def feeds(self) -> list[str]:
        return list(self.outcomes)

    def fetch(self, feed: str, cursor: str | None) -> SyncBatch:
        self.fetched.append((feed, cursor))
        outcome = self.outcomes[feed]
        if isinstance(outcome, Exception):
            raise outcome
        return outcome


def batch(*txns: Any, removed: tuple[str, ...] = (), cursor: str | None = "c1") -> SyncBatch:
    return SyncBatch(tuple(txns), removed, cursor, (CHASE,))


def test_a_successful_sync_stores_transactions_cursor_and_freshness() -> None:
    ledger = MemoryLedger()
    provider = StubProvider({"plaid:a": batch(txn("x1"), txn("x2"))})
    report = run_sync(provider, ledger, ledger, MORNING)
    assert report.outcomes == {"plaid:a": "ok"} and not report.retry
    assert set(ledger.transactions) == {"x1", "x2"}
    assert ledger.cursor("plaid:a") == "c1"
    (status,) = ledger.statuses()
    assert (status.feed, status.last_success, status.last_error, status.accounts) == (
        "plaid:a",
        MORNING.isoformat(),
        None,
        (CHASE,),
    )
    # The next run resumes from the saved cursor.
    run_sync(provider, ledger, ledger, MORNING)
    assert provider.fetched == [("plaid:a", None), ("plaid:a", "c1")]


def test_removed_transactions_leave_the_ledger() -> None:
    ledger = MemoryLedger()
    ledger.upsert([txn("old"), txn("keep")])
    run_sync(StubProvider({"f": batch(removed=("old", "never-stored"))}), ledger, ledger, MORNING)
    assert set(ledger.transactions) == {"keep"}


def test_repeating_a_sync_never_double_counts() -> None:
    ledger = MemoryLedger()
    provider = StubProvider({"f": batch(txn("x1", "10.00"), txn("x2", "20.00"))})
    for _ in range(3):
        run_sync(provider, ledger, ledger, MORNING)
    service = BudgetService(ledger, ledger, lambda: MORNING)
    assert service.month_summary().committed == Decimal("30.00")


def test_an_empty_first_sync_keeps_no_cursor() -> None:
    ledger = MemoryLedger()
    run_sync(StubProvider({"f": batch(cursor=None)}), ledger, ledger, MORNING)
    assert ledger.cursor("f") is None and ledger.statuses()[0].last_error is None


def test_failure_keeps_the_last_success_time_and_records_the_code() -> None:
    ledger = MemoryLedger()
    ledger.save_status(FeedStatus("plaid:a", "2026-10-13T13:00:00+00:00", None, (CHASE,)))
    provider = StubProvider({"plaid:a": PlaidError("ITEM_LOGIN_REQUIRED", retryable=False)})
    report = run_sync(provider, ledger, ledger, MORNING)
    assert report.outcomes == {"plaid:a": "ITEM_LOGIN_REQUIRED"}
    # Needs the household to re-link, so retrying is pointless.
    assert not report.retry
    (status,) = ledger.statuses()
    assert (status.last_success, status.last_error, status.accounts) == (
        "2026-10-13T13:00:00+00:00",
        "ITEM_LOGIN_REQUIRED",
        (CHASE,),
    )


@pytest.mark.parametrize(
    ("error", "retry"),
    [
        (PlaidError("NETWORK_ERROR"), True),
        (PlaidError("INTERNAL_SERVER_ERROR", retryable=True), True),
        (ProviderError("BAD_TRANSACTION_DATA", retryable=False), False),
        (RuntimeError("unexpected"), True),
    ],
)
def test_which_failures_are_worth_retrying(error: Exception, retry: bool) -> None:
    ledger = MemoryLedger()
    report = run_sync(StubProvider({"f": error}), ledger, ledger, MORNING)
    assert report.retry is retry


def test_unexpected_errors_are_recorded_without_their_message() -> None:
    ledger = MemoryLedger()
    run_sync(
        StubProvider({"f": RuntimeError("token access-secret leaked")}), ledger, ledger, MORNING
    )
    assert ledger.statuses()[0].last_error == "SYNC_FAILED"


def test_a_failing_feed_does_not_stop_the_others() -> None:
    ledger = MemoryLedger()
    provider = StubProvider({"bad": PlaidError("NETWORK_ERROR"), "good": batch(txn("x1"))})
    report = run_sync(provider, ledger, ledger, MORNING)
    assert report.outcomes == {"bad": "NETWORK_ERROR", "good": "ok"} and "x1" in ledger.transactions


def test_the_cursor_only_advances_after_the_batch_is_stored() -> None:
    class BrokenLedger(MemoryLedger):
        def upsert(self, transactions: Any) -> None:
            raise RuntimeError("write failed")

    ledger = BrokenLedger()
    report = run_sync(StubProvider({"f": batch(txn("x1"))}), ledger, ledger, MORNING)
    assert report.retry and ledger.cursor("f") is None


def test_a_busy_feed_is_skipped_not_run_twice() -> None:
    ledger = MemoryLedger()
    assert ledger.lock("sync-f", "another-worker", 60)
    provider = StubProvider({"f": batch(txn("x1"))})
    assert run_sync(provider, ledger, ledger, MORNING).outcomes == {"f": "busy"}
    assert not provider.fetched


def test_the_lock_is_released_even_after_a_failure() -> None:
    ledger = MemoryLedger()
    run_sync(StubProvider({"f": RuntimeError("x")}), ledger, ledger, MORNING)
    assert ledger.lock("sync-f", "next", 60)


def test_a_crashed_sync_does_not_block_forever() -> None:
    clock = [1000.0]
    ledger = MemoryLedger(clock=lambda: clock[0])
    assert ledger.lock("sync-f", "crashed", 120)
    assert not ledger.lock("sync-f", "second", 120)
    clock[0] += 121
    assert ledger.lock("sync-f", "second", 120)


def test_only_the_requested_feed_runs() -> None:
    ledger = MemoryLedger()
    provider = StubProvider({"a": batch(txn("x1")), "b": batch(txn("x2"))})
    assert run_sync(provider, ledger, ledger, MORNING, only="b").outcomes == {"b": "ok"}
    assert set(ledger.transactions) == {"x2"}


def test_freshness_is_stored_in_utc() -> None:
    ledger = MemoryLedger()
    central = datetime(2026, 10, 14, 7, 30, tzinfo=HOUSEHOLD_TIME)
    run_sync(StubProvider({"f": batch()}), ledger, ledger, central)
    assert ledger.statuses()[0].last_success == "2026-10-14T12:30:00+00:00"


def test_fixture_provider_runs_through_the_same_path() -> None:
    ledger = MemoryLedger()
    report = run_sync(FixtureProvider(date(2026, 10, 14)), ledger, ledger, MORNING)
    assert report.outcomes == {"fixture": "ok"} and len(ledger.transactions) > 100


# The whole chain with Plaid-shaped data


def test_plaid_data_flows_through_to_the_morning_report() -> None:
    item = PlaidItem("item-1", "access-secret", "Chase", ACCOUNTS)
    pages = [
        page(
            added=[
                raw(
                    transaction_id="food",
                    amount=Decimal("120.00"),
                    date="2026-10-11",
                    authorized_date="2026-10-10",
                    merchant_name="Whole Foods",
                    personal_finance_category={
                        "primary": "FOOD_AND_DRINK",
                        "detailed": "FOOD_AND_DRINK_GROCERIES",
                    },
                ),
                raw(
                    transaction_id="target",
                    amount=Decimal("80.00"),
                    date="2026-10-12",
                    authorized_date="2026-10-12",
                    merchant_name="Target",
                    account_id="acct-rh",
                    personal_finance_category={
                        "primary": "GENERAL_MERCHANDISE",
                        "detailed": "GENERAL_MERCHANDISE_SUPERSTORES",
                    },
                ),
                raw(
                    transaction_id="payment",
                    amount=Decimal("-500.00"),
                    merchant_name=None,
                    name="AUTOMATIC PAYMENT - THANK YOU",
                    personal_finance_category={
                        "primary": "LOAN_PAYMENTS",
                        "detailed": "LOAN_PAYMENTS_CREDIT_CARD_PAYMENT",
                    },
                ),
                raw(transaction_id="checking", account_id="not-a-card", amount=Decimal("999.00")),
            ],
            cursor="cursor-1",
        )
    ]
    ledger = MemoryLedger()
    provider = PlaidProvider(FakeClient(pages), PlaidConfig("c", "s", "sandbox", (item,)))  # type: ignore[arg-type]
    run_sync(provider, ledger, ledger, MORNING)
    summary = BudgetService(ledger, ledger, lambda: MORNING).month_summary()
    assert summary.committed == Decimal("200.00")  # payment and the unlabeled account left out
    # Whole Foods is groceries, and Target counts as groceries by the household convention.
    assert summary.line(Category.GROCERIES).committed == Decimal("200.00")
    text = build_report(BudgetService(ledger, ledger, lambda: MORNING))
    assert "**$200.00** of $6,280.00 (3%)" in text
    assert (
        "Data through Oct 14, 8:00 AM CT for Chase Sapphire Preferred, Robinhood Gold Card." in text
    )
    assert "is not connected" not in text
    assert ledger.cursor("plaid:item-1") == "cursor-1"


# Secret parsing

GOOD = {
    "client_id": "client",
    "secret": "shh",
    "environment": "sandbox",
    "items": [
        {
            "item_id": "item-1",
            "access_token": "access-sandbox-1",
            "institution": "Chase",
            "accounts": {"acct-1": "chase-sapphire-preferred"},
        }
    ],
}


def test_secret_parses_and_hides_credentials_from_repr() -> None:
    import json

    config = parse_plaid_config(json.dumps(GOOD))
    assert config.item("item-1") and config.item("item-1").accounts == {
        "acct-1": "chase-sapphire-preferred"
    }  # type: ignore[union-attr]
    assert config.item("nope") is None
    assert "shh" not in repr(config) and "access-sandbox-1" not in repr(config)


@pytest.mark.parametrize(
    "change",
    [
        {"environment": "development"},
        {"client_id": ""},
        {"secret": ""},
        {"items": [{"item_id": "a b", "access_token": "access-x", "accounts": {}}]},
        {"items": [{"item_id": "a", "access_token": "oops", "accounts": {}}]},
        {"items": [{"item_id": "a", "access_token": "access-x", "accounts": {"x": "Bad Key"}}]},
        {"items": [GOOD["items"][0], GOOD["items"][0]]},
    ],
)
def test_bad_secrets_are_rejected_without_echoing_values(change: dict[str, Any]) -> None:
    import json

    with pytest.raises(ValueError) as caught:
        parse_plaid_config(json.dumps({**GOOD, **change}))
    assert "shh" not in str(caught.value)


@pytest.mark.parametrize("text", ["", "not json", "[]", "null", '{"client_id": 5}'])
def test_garbage_secrets_are_rejected(text: str) -> None:
    with pytest.raises(ValueError):
        parse_plaid_config(text)


def test_secret_is_cached_briefly(monkeypatch: pytest.MonkeyPatch) -> None:
    import json

    from budget_bot import credentials

    calls = []

    class Secrets:
        def get_secret_value(self, SecretId: str) -> dict[str, str]:  # noqa: N803
            calls.append(SecretId)
            return {"SecretString": json.dumps(GOOD)}

    monkeypatch.setattr(credentials.boto3, "client", lambda name: Secrets())
    credentials._plaid_cache.clear()
    plaid_config("household-budget/plaid", now=0)
    plaid_config("household-budget/plaid", now=30)
    assert len(calls) == 1
    plaid_config("household-budget/plaid", now=61)
    assert len(calls) == 2
    # The sync Lambda always asks for the latest copy, so a newly linked card is never missed.
    plaid_config("household-budget/plaid", now=62, fresh=True)
    assert len(calls) == 3
