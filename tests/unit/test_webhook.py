import base64
import json
from typing import Any

import pytest

from budget_bot.discord import DEFERRED_CHANNEL_MESSAGE, EPHEMERAL, PONG
from budget_bot.webhook import handle
from tests.helpers import (
    ALICE,
    INTERACTION_TOKEN,
    NOW,
    STRANGER,
    Queue,
    interaction,
    ping,
    settings,
    signed,
)


def reply(response: dict[str, Any]) -> dict[str, Any]:
    assert response["statusCode"] == 200
    assert response["headers"] == {"Content-Type": "application/json"}
    body: dict[str, Any] = json.loads(response["body"])
    return body


def test_ping_is_acknowledged() -> None:
    queue = Queue()
    assert reply(handle(signed(ping()), queue, settings(), now=NOW)) == {"type": PONG}
    assert not queue.messages


def test_live_question_is_queued_and_deferred() -> None:
    queue = Queue()
    response = handle(signed(interaction()), queue, settings(live=True), now=NOW)
    assert reply(response) == {"type": DEFERRED_CHANNEL_MESSAGE}
    assert queue.messages == [
        {
            "interaction_id": "200000000000000001",
            "user_id": ALICE,
            "text": "How much have we spent this month?",
            "token": INTERACTION_TOKEN,
        }
    ]


def test_dry_run_question_is_queued_with_private_notice() -> None:
    queue = Queue()
    body = reply(handle(signed(interaction()), queue, settings(), now=NOW))
    assert body["data"]["flags"] == EPHEMERAL
    assert "dry-run" in body["data"]["content"]
    assert len(queue.messages) == 1


@pytest.mark.parametrize(
    "raw",
    [
        interaction(user=STRANGER),
        interaction(channel="100000000000000099"),
        interaction(guild="100000000000000099"),
        interaction(application="100000000000000099"),
        interaction(name="other"),
    ],
)
def test_outside_household_gets_private_refusal(raw: bytes) -> None:
    queue = Queue()
    body = reply(handle(signed(raw), queue, settings(live=True), now=NOW))
    assert body["data"]["flags"] == EPHEMERAL
    assert not queue.messages


@pytest.mark.parametrize(
    "change",
    [
        lambda event: event.update(body=event["body"].replace("month", "week")),
        lambda event: event["headers"].update({"X-Signature-Timestamp": str(NOW - 301)}),
        lambda event: event["headers"].update({"X-Signature-Ed25519": "bad"}),
        lambda event: event.update(body=None),
        lambda event: event.update(body=42),
    ],
)
def test_bad_signature_or_replay_is_unauthorized(change: Any) -> None:
    event = signed(interaction())
    change(event)
    queue = Queue()
    assert handle(event, queue, settings(), now=NOW)["statusCode"] == 401
    assert not queue.messages


@pytest.mark.parametrize("raw", [b"[]", b"{bad json", json.dumps({"type": 3}).encode()])
def test_signed_but_malformed_interaction_is_rejected(raw: bytes) -> None:
    queue = Queue()
    assert handle(signed(raw), queue, settings(), now=NOW)["statusCode"] == 400
    assert not queue.messages


def test_long_question_is_truncated_not_rejected() -> None:
    queue = Queue()
    handle(signed(interaction(question="q" * 600)), queue, settings(), now=NOW)
    assert queue.messages[0]["text"] == "q" * 500


def test_base64_body_preserves_signature() -> None:
    raw = interaction()
    event = signed(raw)
    event.update(body=base64.b64encode(raw).decode(), isBase64Encoded=True)
    queue = Queue()
    reply(handle(event, queue, settings(), now=NOW))
    assert len(queue.messages) == 1


def test_queue_error_fails_the_request() -> None:
    class BrokenQueue(Queue):
        def send_message(self, **kwargs: Any) -> None:
            raise RuntimeError("SQS unavailable")

    with pytest.raises(RuntimeError, match="SQS unavailable"):
        handle(signed(interaction()), BrokenQueue(), settings(), now=NOW)


@pytest.mark.parametrize(
    ("changes", "status"),
    [
        ({"requestContext": {"http": {"method": "GET"}}}, 405),
        ({"body": "x" * 256001}, 413),
        ({"body": "!bad base64", "isBase64Encoded": True}, 401),
    ],
)
def test_method_and_size_limits(changes: dict[str, Any], status: int) -> None:
    event = signed(interaction())
    event.update(changes)
    queue = Queue()
    assert handle(event, queue, settings(), now=NOW)["statusCode"] == status
    assert not queue.messages
