"""Discord interaction verification, parsing, responses and HTTP adapters."""

import json
import re
import time
import urllib.parse
import urllib.request
from collections.abc import Mapping
from dataclasses import dataclass, field
from typing import Any

from cryptography.exceptions import InvalidSignature
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PublicKey

API = "https://discord.com/api/v10"
# Discord rejects API requests that do not send a bot-style User-Agent.
USER_AGENT = "DiscordBot (https://github.com/Alex-Watt/household-budget-assistant, 0.2)"
WEBHOOK_URL = re.compile(r"https://discord\.com/api/webhooks/[0-9]{17,20}/[A-Za-z0-9_-]{1,100}\Z")
MAX_MESSAGE = 2000
# Keep links in a reply (for example a merchant like "COSTCO.COM") from unfurling into previews.
SUPPRESS_EMBEDS = 1 << 2
ASK_COMMAND = "ask"

PING = 1
APPLICATION_COMMAND = 2
PONG = 1
CHANNEL_MESSAGE = 4
DEFERRED_CHANNEL_MESSAGE = 5
EPHEMERAL = 1 << 6


class RejectedRequest(ValueError):
    pass


@dataclass(frozen=True)
class Ping:
    pass


@dataclass(frozen=True)
class Command:
    interaction_id: str
    application_id: str
    guild_id: str
    channel_id: str
    user_id: str
    name: str
    question: str = field(repr=False)
    token: str = field(repr=False)


def verify_request(
    raw: bytes, headers: Mapping[str, str], public_key_hex: str, now: int | None = None
) -> None:
    lowered = {key.lower(): value for key, value in headers.items()}
    timestamp = lowered.get("x-signature-timestamp", "")
    signature = lowered.get("x-signature-ed25519", "")
    if (
        not isinstance(timestamp, str)
        or not timestamp.isascii()
        or not timestamp.isdecimal()
        or not isinstance(signature, str)
        or not signature
    ):
        raise RejectedRequest("missing signature or timestamp")
    current = int(time.time()) if now is None else now
    if abs(current - int(timestamp)) > 300:
        raise RejectedRequest("timestamp outside five-minute window")
    try:
        key = Ed25519PublicKey.from_public_bytes(bytes.fromhex(public_key_hex))
        key.verify(bytes.fromhex(signature), timestamp.encode() + raw)
    except (ValueError, InvalidSignature) as exc:
        raise RejectedRequest("invalid signature") from exc


def parse_interaction(raw: bytes) -> Ping | Command:
    try:
        event: dict[str, Any] = json.loads(raw)
        kind = event["type"]
        if type(kind) is not int:
            raise RejectedRequest("invalid interaction type")
        if kind == PING:
            return Ping()
        if kind != APPLICATION_COMMAND:
            raise RejectedRequest("unsupported interaction type")
        data = event["data"]
        options = {option["name"]: option.get("value") for option in data.get("options", [])}
        command = Command(
            interaction_id=event["id"],
            application_id=event["application_id"],
            guild_id=event["guild_id"],
            channel_id=event["channel_id"],
            user_id=event["member"]["user"]["id"],
            name=data["name"],
            question=options.get("question", ""),
            token=event["token"],
        )
    except (KeyError, TypeError, ValueError, AttributeError) as exc:
        raise RejectedRequest("malformed interaction") from exc
    if not all(isinstance(value, str) for value in vars(command).values()):
        raise RejectedRequest("invalid interaction fields")
    return command


def _message(text: str) -> dict[str, Any]:
    return {
        "content": text[:MAX_MESSAGE],
        "allowed_mentions": {"parse": []},
        "flags": SUPPRESS_EMBEDS,
    }


def pong() -> dict[str, Any]:
    return {"type": PONG}


def deferred() -> dict[str, Any]:
    return {"type": DEFERRED_CHANNEL_MESSAGE}


def private_message(text: str) -> dict[str, Any]:
    return {"type": CHANNEL_MESSAGE, "data": {**_message(text), "flags": EPHEMERAL}}


def _send(method: str, url: str, text: str, timeout: float) -> None:
    request = urllib.request.Request(
        url,
        data=json.dumps(_message(text)).encode(),
        method=method,
        headers={"Content-Type": "application/json", "User-Agent": USER_AGENT},
    )
    with urllib.request.urlopen(request, timeout=timeout) as response:
        response.read()


class DiscordReplies:
    """Edits the deferred reply to a slash command; uses the interaction token, not a bot token."""

    def __init__(self, application_id: str, timeout: float = 8.0) -> None:
        self.application_id = application_id
        self.timeout = timeout

    def edit_reply(self, token: str, text: str) -> None:
        if not text:
            raise ValueError("empty reply")
        path = f"{self.application_id}/{urllib.parse.quote(token, safe='')}"
        _send("PATCH", f"{API}/webhooks/{path}/messages/@original", text, self.timeout)


class DiscordChannelWebhook:
    """Posts to the one channel its webhook URL belongs to."""

    def __init__(self, url: str, timeout: float = 8.0) -> None:
        if not WEBHOOK_URL.fullmatch(url):
            raise ValueError("not a discord.com channel webhook URL")
        self.url = url
        self.timeout = timeout

    def post(self, text: str) -> None:
        if not text:
            raise ValueError("empty message")
        _send("POST", f"{self.url}?wait=true", text, self.timeout)


class MockDiscord:
    def __init__(self) -> None:
        self.replies: list[tuple[str, str]] = []
        self.posts: list[str] = []

    def edit_reply(self, token: str, text: str) -> None:
        self.replies.append((token, text))

    def post(self, text: str) -> None:
        self.posts.append(text)
