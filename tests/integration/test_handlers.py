"""The real Lambda entry points against emulated DynamoDB, with only the network faked."""

import json
from collections.abc import Iterator
from datetime import UTC, date, datetime
from decimal import Decimal
from typing import Any

import boto3
import pytest
from moto import mock_aws

from budget_bot import plaid_webhook, report, sync, worker
from budget_bot.credentials import PlaidConfig, PlaidItem
from budget_bot.discord import MockDiscord
from budget_bot.ledger import DynamoLedger
from budget_bot.models import Question
from budget_bot.ports import FeedStatus
from tests.helpers import ALICE, CHASE, INTERACTION_TOKEN, ScriptedLlm, text_reply, tool_reply, txn
from tests.integration.test_ledger import create_ledger_table
from tests.unit.test_plaid import ACCOUNTS, FakeClient, page, raw
from tests.unit.test_plaid_webhook import BODY, jwk, sign

pytestmark = pytest.mark.integration

GROCERY = {"primary": "FOOD_AND_DRINK", "detailed": "FOOD_AND_DRINK_GROCERIES"}
PINNED = datetime(2026, 10, 14, 13, 0, tzinfo=UTC)  # 8 AM Central


class FrozenDatetime(datetime):
    """Lets a test choose what `datetime.now(tz)` returns inside a Lambda module."""

    @classmethod
    def now(cls, tz: Any = None) -> datetime:
        return PINNED.astimezone(tz) if tz else PINNED


@pytest.fixture
def aws() -> Iterator[dict[str, Any]]:
    with mock_aws():
        resource = boto3.resource("dynamodb", region_name="us-east-2")
        state = resource.create_table(
            TableName="messaging-state",
            KeySchema=[{"AttributeName": "pk", "KeyType": "HASH"}],
            AttributeDefinitions=[{"AttributeName": "pk", "AttributeType": "S"}],
            BillingMode="PAY_PER_REQUEST",
        )
        yield {"state": state, "ledger": create_ledger_table(resource)}


def seed(aws: dict[str, Any]) -> DynamoLedger:
    ledger = DynamoLedger(aws["ledger"])
    ledger.upsert([txn("a", "120.00", date(2026, 10, 3), "Aldi", **GROCERY)])
    ledger.save_status(FeedStatus("plaid:item-1", PINNED.isoformat(), None, (CHASE,)))
    return ledger


def question_event(text: str, interaction_id: str = "300") -> dict[str, Any]:
    body = Question(interaction_id, ALICE, text, INTERACTION_TOKEN).to_dict()
    return {"Records": [{"messageId": "m1", "body": json.dumps(body)}]}


def live(monkeypatch: pytest.MonkeyPatch) -> MockDiscord:
    discord = MockDiscord()
    monkeypatch.setenv("LIVE_MESSAGING_ENABLED", "true")
    monkeypatch.setattr(worker, "DiscordReplies", lambda _application_id: discord)
    return discord


class Context:
    """The part of the Lambda context the worker uses to know how long it has left."""

    def __init__(self, seconds: float) -> None:
        self.seconds = seconds

    def get_remaining_time_in_millis(self) -> int:
        return int(self.seconds * 1000)


# Worker


def test_worker_answers_a_command_from_the_real_ledger(
    aws: dict[str, Any], monkeypatch: pytest.MonkeyPatch
) -> None:
    seed(aws)
    discord = live(monkeypatch)
    monkeypatch.setattr(worker, "datetime", FrozenDatetime)
    assert worker.lambda_handler(question_event("summary"), None) == {"batchItemFailures": []}
    ((token, text),) = discord.replies
    assert token == INTERACTION_TOKEN
    assert text.startswith("Month to date: $120.00 of $6,280.00 (2%) spent")
    # A redelivered message is acknowledged without a second reply.
    worker.lambda_handler(question_event("summary"), None)
    assert len(discord.replies) == 1


def test_worker_without_card_data_says_so(
    aws: dict[str, Any], monkeypatch: pytest.MonkeyPatch
) -> None:
    discord = live(monkeypatch)
    worker.lambda_handler(question_event("summary"), None)
    assert (
        discord.replies[0][1]
        == "Card data isn't connected yet, so there are no spending totals to report."
    )


def test_worker_dry_run_reads_nothing_and_replies_nothing(aws: dict[str, Any]) -> None:
    assert worker.lambda_handler(question_event("summary"), None) == {"batchItemFailures": []}


def test_worker_uses_the_model_through_tools_and_counts_the_daily_limit(
    aws: dict[str, Any], monkeypatch: pytest.MonkeyPatch
) -> None:
    seed(aws)
    discord = live(monkeypatch)
    monkeypatch.setenv("AI_ENABLED", "true")
    monkeypatch.setenv("AI_DAILY_LIMIT", "1")
    llm = ScriptedLlm(
        [
            tool_reply(("search_transactions", {"merchant": "aldi", "period": "this_month"})),
            text_reply("One Aldi purchase, $120.00 in total."),
        ]
    )
    monkeypatch.setattr(worker, "_model", lambda _scope: llm)
    question = "What did we spend at Aldi this month?"
    # The seeded purchase is dated 3 October; pin the clock so "this month" includes it.
    monkeypatch.setattr(worker, "datetime", FrozenDatetime)
    worker.lambda_handler(question_event(question, "301"), Context(90))
    assert discord.replies[0][1] == "One Aldi purchase, $120.00 in total."
    # The second AI question today is over the limit of one, but commands still work.
    worker.lambda_handler(question_event(question, "302"), Context(90))
    assert "limit for AI answers" in discord.replies[1][1]
    worker.lambda_handler(question_event("summary", "303"), Context(90))
    assert discord.replies[2][1].startswith("Month to date:")
    assert len(llm.calls) == 2


def test_worker_gives_up_on_the_model_when_the_lambda_is_nearly_out_of_time(
    aws: dict[str, Any], monkeypatch: pytest.MonkeyPatch
) -> None:
    seed(aws)
    discord = live(monkeypatch)
    monkeypatch.setenv("AI_ENABLED", "true")
    llm = ScriptedLlm([text_reply("never reached")])
    monkeypatch.setattr(worker, "_model", lambda _scope: llm)
    monkeypatch.setattr(worker, "datetime", FrozenDatetime)
    worker.lambda_handler(question_event("What did we spend at Aldi this month?"), Context(20))
    assert not llm.calls
    assert discord.replies[0][1].startswith("My AI helper isn't available right now")


# Report


def test_report_lambda_dry_run_wiring(aws: dict[str, Any]) -> None:
    seed(aws)
    assert report.lambda_handler({}, None) == {"outcome": "dry_run"}
    assert report.lambda_handler({}, None) == {"outcome": "duplicate"}


def test_report_lambda_posts_real_totals(
    aws: dict[str, Any], monkeypatch: pytest.MonkeyPatch
) -> None:
    DynamoLedger(aws["ledger"]).upsert([])
    discord = MockDiscord()
    monkeypatch.setenv("LIVE_MESSAGING_ENABLED", "true")
    monkeypatch.setattr(
        report,
        "report_webhook_url",
        lambda _name: "https://discord.com/api/webhooks/100000000000000001/abc",
    )
    monkeypatch.setattr(report, "DiscordChannelWebhook", lambda _url: discord)
    assert report.lambda_handler({}, None) == {"outcome": "sent"}
    # Nothing has synced, so the post says so rather than showing zero spending.
    assert "No card data yet, so no spending totals." in discord.posts[0]


# Sync


def plaid_config_for_tests() -> PlaidConfig:
    item = PlaidItem("item-1", "access-secret", "Chase", ACCOUNTS)
    return PlaidConfig("client", "secret", "sandbox", (item,))


def test_sync_is_a_no_op_until_plaid_is_enabled(aws: dict[str, Any]) -> None:
    assert sync.lambda_handler({}, None) == {"outcome": "disabled"}
    assert DynamoLedger(aws["ledger"]).statuses() == []


def test_sync_pulls_transactions_and_records_freshness(
    aws: dict[str, Any], monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("PLAID_ENABLED", "true")
    monkeypatch.setattr(sync, "plaid_config", lambda _name, **_kw: plaid_config_for_tests())
    pages = [
        page(
            [
                raw(
                    transaction_id="t1",
                    amount=Decimal("19.99"),
                    date="2026-10-13",
                    authorized_date="2026-10-12",
                )
            ],
            cursor="c1",
        )
    ]
    monkeypatch.setattr(sync, "PlaidClient", lambda *_args: FakeClient(pages))
    assert sync.lambda_handler({}, None) == {"outcome": "ok", "feeds": 1}
    ledger = DynamoLedger(aws["ledger"])
    assert ledger.get("plaid:t1").amount == Decimal("19.99")  # type: ignore[union-attr]
    assert ledger.cursor("plaid:item-1") == "c1"
    (status,) = ledger.statuses()
    assert status.feed == "plaid:item-1" and status.last_error is None


def test_sync_always_reads_the_latest_secret(
    aws: dict[str, Any], monkeypatch: pytest.MonkeyPatch
) -> None:
    asked: list[dict[str, Any]] = []

    def config(_name: str, **kwargs: Any) -> PlaidConfig:
        asked.append(kwargs)
        return plaid_config_for_tests()

    monkeypatch.setenv("PLAID_ENABLED", "true")
    monkeypatch.setattr(sync, "plaid_config", config)
    monkeypatch.setattr(sync, "PlaidClient", lambda *_args: FakeClient([page(cursor="c")]))
    sync.lambda_handler({}, None)
    assert asked == [{"fresh": True}]


def test_sync_ignores_webhooks_for_unlinked_items(
    aws: dict[str, Any], monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("PLAID_ENABLED", "true")
    monkeypatch.setattr(sync, "plaid_config", lambda _name, **_kw: plaid_config_for_tests())
    monkeypatch.setattr(sync, "PlaidClient", lambda *_args: FakeClient([]))
    assert sync.lambda_handler({"feed": "plaid:removed"}, None) == {"outcome": "ignored"}


def test_sync_raises_for_retryable_failures_so_scheduler_retries(
    aws: dict[str, Any], monkeypatch: pytest.MonkeyPatch
) -> None:
    from budget_bot.plaid import PlaidError

    monkeypatch.setenv("PLAID_ENABLED", "true")
    monkeypatch.setattr(sync, "plaid_config", lambda _name, **_kw: plaid_config_for_tests())
    monkeypatch.setattr(
        sync, "PlaidClient", lambda *_args: FakeClient([PlaidError("NETWORK_ERROR")])
    )
    with pytest.raises(RuntimeError, match="retryable"):
        sync.lambda_handler({}, None)
    assert DynamoLedger(aws["ledger"]).statuses()[0].last_error == "NETWORK_ERROR"


def test_sync_records_but_does_not_retry_a_login_problem(
    aws: dict[str, Any], monkeypatch: pytest.MonkeyPatch
) -> None:
    from budget_bot.plaid import PlaidError

    monkeypatch.setenv("PLAID_ENABLED", "true")
    monkeypatch.setattr(sync, "plaid_config", lambda _name, **_kw: plaid_config_for_tests())
    relink = PlaidError("ITEM_LOGIN_REQUIRED", retryable=False)
    monkeypatch.setattr(sync, "PlaidClient", lambda *_args: FakeClient([relink]))
    assert sync.lambda_handler({}, None) == {"outcome": "partial", "feeds": 0}
    assert DynamoLedger(aws["ledger"]).statuses()[0].last_error == "ITEM_LOGIN_REQUIRED"


def test_a_webhook_triggered_sync_waits_for_one_already_running(
    aws: dict[str, Any], monkeypatch: pytest.MonkeyPatch
) -> None:
    ledger = DynamoLedger(aws["ledger"])
    ledger.lock("sync-plaid:item-1", "another-run", sync.LOCK_SECONDS)
    sleeps: list[float] = []

    def sleep(seconds: float) -> None:
        sleeps.append(seconds)
        if len(sleeps) == 2:  # the other run finishes while we wait
            ledger.unlock("sync-plaid:item-1", "another-run")

    monkeypatch.setattr(sync.time, "sleep", sleep)
    monkeypatch.setenv("PLAID_ENABLED", "true")
    monkeypatch.setattr(sync, "plaid_config", lambda _name, **_kw: plaid_config_for_tests())
    monkeypatch.setattr(sync, "PlaidClient", lambda *_args: FakeClient([page(cursor="c9")]))
    assert sync.lambda_handler({"feed": "plaid:item-1"}, None) == {"outcome": "ok", "feeds": 1}
    assert sleeps == [sync.LOCK_POLL_SECONDS] * 2
    assert ledger.cursor("plaid:item-1") == "c9"


# Plaid webhook receiver


def hook_event() -> dict[str, Any]:
    return {
        "requestContext": {"http": {"method": "POST"}},
        "headers": {"plaid-verification": sign(BODY, iat=int(datetime.now(UTC).timestamp()))},
        "body": BODY.decode(),
    }


def test_plaid_webhook_is_closed_until_plaid_is_enabled() -> None:
    assert plaid_webhook.lambda_handler(hook_event(), None)["statusCode"] == 404


def test_plaid_webhook_verifies_then_starts_the_sync_lambda(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    for cache in (plaid_webhook._keys, plaid_webhook._failed, plaid_webhook._lookups):
        cache.clear()
    monkeypatch.setenv("PLAID_ENABLED", "true")
    item = PlaidItem("item-1", "access-secret", "Chase", ACCOUNTS)
    monkeypatch.setattr(
        plaid_webhook, "plaid_config", lambda _name: PlaidConfig("c", "s", "sandbox", (item,))
    )
    timeouts: list[float] = []

    class Client:
        def __init__(self, *_args: Any, timeout: float = 20.0) -> None:
            timeouts.append(timeout)

        def webhook_verification_key(self, key_id: str) -> dict[str, Any]:
            return {"key": jwk()}

    invoked: list[dict[str, Any]] = []

    class Lambda:
        def invoke(self, **kwargs: Any) -> dict[str, Any]:
            invoked.append(kwargs)
            return {}

    monkeypatch.setattr(plaid_webhook, "PlaidClient", Client)
    monkeypatch.setattr(plaid_webhook, "_lambda_client", lambda: Lambda())
    assert plaid_webhook.lambda_handler(hook_event(), None)["statusCode"] == 200
    (call,) = invoked
    assert call["FunctionName"] == "sync-function" and call["InvocationType"] == "Event"
    assert json.loads(call["Payload"]) == {"feed": "plaid:item-1"}
    # The public endpoint uses a short Plaid timeout so a flood cannot tie up the function.
    assert timeouts and timeouts[0] <= 5
    forged = {**hook_event(), "headers": {"plaid-verification": "a.b.c"}}
    assert plaid_webhook.lambda_handler(forged, None)["statusCode"] == 401
    assert len(invoked) == 1
