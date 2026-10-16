import json
from typing import Any

import pytest
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

from budget_bot.discord import (
    Command,
    DiscordChannelWebhook,
    DiscordReplies,
    Ping,
    RejectedRequest,
    parse_interaction,
    verify_request,
)
from tests.helpers import (
    ALICE,
    APP_ID,
    BOB,
    NOW,
    PUBLIC_KEY,
    interaction,
    ping,
    public_key,
    signed,
)

WEBHOOK = "https://discord.com/api/webhooks/300000000000000001/abc_DEF-123"


def test_signature_covers_timestamp_and_raw_body() -> None:
    raw = interaction()
    headers = signed(raw)["headers"]
    verify_request(raw, headers, PUBLIC_KEY, now=NOW)
    with pytest.raises(RejectedRequest):
        verify_request(raw + b" ", headers, PUBLIC_KEY, now=NOW)
    with pytest.raises(RejectedRequest):
        verify_request(raw, headers, public_key(Ed25519PrivateKey.generate()), now=NOW)


@pytest.mark.parametrize("timestamp", [NOW - 301, NOW + 301])
def test_timestamp_window_in_both_directions(timestamp: int) -> None:
    raw = interaction()
    with pytest.raises(RejectedRequest):
        verify_request(raw, signed(raw, timestamp=timestamp)["headers"], PUBLIC_KEY, now=NOW)


@pytest.mark.parametrize(
    "headers",
    [
        {},
        {"X-Signature-Timestamp": str(NOW)},
        {"X-Signature-Timestamp": "soon", "X-Signature-Ed25519": "00"},
        {"X-Signature-Timestamp": str(NOW), "X-Signature-Ed25519": "not hex"},
    ],
)
def test_missing_or_malformed_headers(headers: dict[str, str]) -> None:
    with pytest.raises(RejectedRequest):
        verify_request(interaction(), headers, PUBLIC_KEY, now=NOW)


def test_ping_and_each_spouse_command() -> None:
    assert parse_interaction(ping()) == Ping()
    for user in (ALICE, BOB):
        command = parse_interaction(interaction(user=user, question="Groceries?"))
        assert isinstance(command, Command)
        assert (command.user_id, command.name, command.question) == (user, "ask", "Groceries?")


@pytest.mark.parametrize(
    "raw",
    [
        b"null",
        b"[]",
        b"{}",
        b"invalid",
        json.dumps({"type": True}).encode(),
        json.dumps({"type": 3, "data": {}}).encode(),
        interaction().replace(b'"member"', b'"user"'),
        interaction().replace(b'"How much have we spent this month?"', b"42"),
    ],
)
def test_malformed_or_unsupported_interactions(raw: bytes) -> None:
    with pytest.raises(RejectedRequest):
        parse_interaction(raw)


class Capture:
    def __init__(self) -> None:
        self.requests: list[Any] = []

    def __call__(self, request: Any, timeout: float) -> Capture:
        assert timeout == 8.0
        self.requests.append(request)
        return self

    def __enter__(self) -> Capture:
        return self

    def __exit__(self, *_args: Any) -> None:
        pass

    def read(self, *_args: Any) -> bytes:
        return b"{}"


def test_reply_edits_original_without_pinging(monkeypatch: pytest.MonkeyPatch) -> None:
    capture = Capture()
    monkeypatch.setattr("budget_bot.discord.urllib.request.urlopen", capture)
    DiscordReplies(APP_ID).edit_reply("tok/en?", "x" * 2500)
    request = capture.requests[0]
    assert request.get_method() == "PATCH"
    assert request.full_url == (
        f"https://discord.com/api/v10/webhooks/{APP_ID}/tok%2Fen%3F/messages/@original"
    )
    assert request.get_header("User-agent").startswith("DiscordBot (")
    body = json.loads(request.data)
    # Flag 4 keeps links in a reply from unfurling into previews.
    assert body == {"content": "x" * 2000, "allowed_mentions": {"parse": []}, "flags": 4}


def test_channel_webhook_waits_for_confirmation(monkeypatch: pytest.MonkeyPatch) -> None:
    capture = Capture()
    monkeypatch.setattr("budget_bot.discord.urllib.request.urlopen", capture)
    DiscordChannelWebhook(WEBHOOK).post("Good morning")
    request = capture.requests[0]
    assert (request.get_method(), request.full_url) == ("POST", f"{WEBHOOK}?wait=true")
    assert json.loads(request.data)["allowed_mentions"] == {"parse": []}


@pytest.mark.parametrize(
    "url",
    [
        "",
        "https://example.com/api/webhooks/300000000000000001/abc",
        "http://discord.com/api/webhooks/300000000000000001/abc",
        "https://discord.com/api/webhooks/300000000000000001/abc?thread_id=1",
        "https://discord.com.evil.test/api/webhooks/300000000000000001/abc",
    ],
)
def test_channel_webhook_only_accepts_discord_urls(url: str) -> None:
    with pytest.raises(ValueError):
        DiscordChannelWebhook(url)
