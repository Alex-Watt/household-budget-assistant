"""All tests use fake configuration and block unmocked network calls."""

from typing import Any

import pytest

from budget_bot import credentials, webhook, worker
from budget_bot.credentials import report_webhook_url
from budget_bot.ledger import ledger_table
from budget_bot.state import state_table
from tests.helpers import ALICE, APP_ID, BOB, CHANNEL_ID, GUILD_ID, PUBLIC_KEY


@pytest.fixture(autouse=True)
def test_environment(monkeypatch: pytest.MonkeyPatch) -> None:
    values = {
        "DISCORD_APPLICATION_ID": APP_ID,
        "DISCORD_PUBLIC_KEY": PUBLIC_KEY,
        "DISCORD_GUILD_ID": GUILD_ID,
        "DISCORD_CHANNEL_ID": CHANNEL_ID,
        "DISCORD_MEMBER_1_ID": ALICE,
        "DISCORD_MEMBER_2_ID": BOB,
        "DISCORD_SECRET_NAME": "household-budget/discord",
        "QUEUE_URL": "https://sqs.example/test",
        "STATE_TABLE": "messaging-state",
        "LEDGER_TABLE": "ledger",
        "AI_ENABLED": "false",
        "PLAID_ENABLED": "false",
        "SYNC_FUNCTION": "sync-function",
        "LIVE_MESSAGING_ENABLED": "false",
        "AWS_ACCESS_KEY_ID": "testing",
        "AWS_SECRET_ACCESS_KEY": "testing",
        "AWS_SESSION_TOKEN": "testing",
        "AWS_DEFAULT_REGION": "us-east-2",
        "AWS_EC2_METADATA_DISABLED": "true",
    }
    for key, value in values.items():
        monkeypatch.setenv(key, value)
    for cached in (report_webhook_url, webhook._sqs, state_table, ledger_table, worker._model):
        cached.cache_clear()
    credentials._plaid_cache.clear()

    def blocked(*args: Any, **kwargs: Any) -> None:
        raise AssertionError("An unmocked network call was attempted")

    monkeypatch.setattr("urllib.request.urlopen", blocked)
    monkeypatch.setattr("botocore.httpsession.URLLib3Session.send", blocked)
