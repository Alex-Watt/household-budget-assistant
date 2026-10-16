from typing import Any

import pytest

from budget_bot import report, worker
from budget_bot.credentials import report_webhook_url
from budget_bot.webhook import lambda_handler as webhook_handler

WEBHOOK = "https://discord.com/api/webhooks/300000000000000001/abc_DEF-123"


def secrets_returning(value: Any) -> Any:
    class Secrets:
        def get_secret_value(self, **kwargs: Any) -> dict[str, Any]:
            return {"SecretString": value}

    return lambda _service: Secrets()


@pytest.mark.parametrize(
    "value",
    [None, "", '{"report_webhook_url": "x"}', "https://example.com/api/webhooks/1/abc"],
)
def test_secret_must_be_a_discord_webhook_url(monkeypatch: pytest.MonkeyPatch, value: Any) -> None:
    monkeypatch.setattr("budget_bot.credentials.boto3.client", secrets_returning(value))
    with pytest.raises(ValueError, match="discord.com channel webhook"):
        report_webhook_url("test-secret")


def test_secret_url_is_trimmed(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr("budget_bot.credentials.boto3.client", secrets_returning(f" {WEBHOOK}\n"))
    assert report_webhook_url("test-secret") == WEBHOOK


class Dynamo:
    def Table(self, name: str) -> object:
        return object()


def forbidden(*args: Any, **kwargs: Any) -> None:
    raise AssertionError("dry run must not request credentials")


def test_dry_run_entry_points_do_not_fetch_secret(monkeypatch: pytest.MonkeyPatch) -> None:
    class State:
        def __init__(self, table: object) -> None:
            pass

        def claim(self, message_id: str) -> str:
            return "lease"

        def complete(self, message_id: str, token: str) -> None:
            pass

    monkeypatch.setenv("STATE_TABLE", "test-state")
    monkeypatch.setattr("budget_bot.state.boto3.resource", lambda _: Dynamo())
    monkeypatch.setattr("budget_bot.report.report_webhook_url", forbidden)
    monkeypatch.setattr("budget_bot.report.DynamoMessagingState", State)
    assert worker.lambda_handler({"Records": []}, None) == {"batchItemFailures": []}
    assert report.lambda_handler({}, None) == {"outcome": "dry_run"}


def test_webhook_never_reads_secrets(monkeypatch: pytest.MonkeyPatch) -> None:
    def client(service: str) -> object:
        assert service == "sqs", "webhook must only use SQS"
        return object()

    monkeypatch.setattr("budget_bot.webhook.boto3.client", client)
    event = {"requestContext": {"http": {"method": "POST"}}, "body": "{}", "headers": {}}
    assert webhook_handler(event, None)["statusCode"] == 401
