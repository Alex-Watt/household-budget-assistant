"""Synthetic Discord interactions and small in-memory ports for unit tests."""

import json
from datetime import UTC, date, datetime, timedelta
from decimal import Decimal
from typing import Any

from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

from budget_bot.budget import BudgetService
from budget_bot.config import Settings
from budget_bot.ledger_memory import MemoryLedger
from budget_bot.ports import FeedStatus, MessageInProgress
from budget_bot.transactions import Transaction

APP_ID = "100000000000000001"
GUILD_ID = "100000000000000002"
CHANNEL_ID = "100000000000000003"
ALICE = "100000000000000004"
BOB = "100000000000000005"
STRANGER = "100000000000000006"
INTERACTION_TOKEN = "aW50ZXJhY3Rpb246dGVzdA"
NOW = 1_800_000_000
SIGNING_KEY = Ed25519PrivateKey.generate()


def public_key(key: Ed25519PrivateKey = SIGNING_KEY) -> str:
    raw = key.public_key().public_bytes(
        encoding=serialization.Encoding.Raw, format=serialization.PublicFormat.Raw
    )
    return raw.hex()


PUBLIC_KEY = public_key()


def settings(live: bool = False, key: str = PUBLIC_KEY) -> Settings:
    return Settings(APP_ID, key, GUILD_ID, CHANNEL_ID, (ALICE, BOB), live_messaging=live)


def interaction(
    user: str = ALICE,
    channel: str = CHANNEL_ID,
    guild: str = GUILD_ID,
    application: str = APP_ID,
    name: str = "ask",
    question: str = "How much have we spent this month?",
) -> bytes:
    return json.dumps(
        {
            "id": "200000000000000001",
            "application_id": application,
            "type": 2,
            "token": INTERACTION_TOKEN,
            "guild_id": guild,
            "channel_id": channel,
            "member": {"user": {"id": user, "username": "spouse"}},
            "data": {
                "name": name,
                "type": 1,
                "options": [{"name": "question", "type": 3, "value": question}],
            },
        }
    ).encode()


def ping() -> bytes:
    return json.dumps({"id": "200000000000000002", "application_id": APP_ID, "type": 1}).encode()


def signed(
    raw: bytes, key: Ed25519PrivateKey = SIGNING_KEY, timestamp: int = NOW
) -> dict[str, Any]:
    signature = key.sign(str(timestamp).encode() + raw)
    return {
        "requestContext": {"http": {"method": "POST"}},
        "body": raw.decode(),
        "headers": {
            "X-Signature-Timestamp": str(timestamp),
            "X-Signature-Ed25519": signature.hex(),
        },
    }


class Queue:
    def __init__(self) -> None:
        self.messages: list[dict[str, Any]] = []

    def send_message(self, **kwargs: Any) -> None:
        self.messages.append(json.loads(kwargs["MessageBody"]))


class MemoryState:
    def __init__(self) -> None:
        self.claims: dict[str, str] = {}
        self.completed: set[str] = set()

    def claim(self, message_id: str) -> str | None:
        if message_id in self.completed:
            return None
        if message_id in self.claims:
            raise MessageInProgress("busy")
        self.claims[message_id] = "test-token"
        return "test-token"

    def complete(self, message_id: str, token: str) -> None:
        assert self.claims.pop(message_id) == token
        self.completed.add(message_id)

    def release(self, message_id: str, token: str) -> None:
        self.claims.pop(message_id, None)

    def recall_context(self) -> dict[str, Any] | None:
        return getattr(self, "_context", None)

    def save_context(self, data: dict[str, Any]) -> None:
        self._context = data


CHASE = "chase-sapphire-preferred"
ROBINHOOD = "robinhood-gold-card"


def txn(
    txn_id: str = "t1",
    amount: str = "10.00",
    spend: date = date(2026, 10, 5),
    merchant: str = "Corner Shop",
    primary: str | None = None,
    detailed: str | None = None,
    pending: bool = False,
    account: str = CHASE,
    feed: str = "plaid:item1",
    posted: date | None = None,
    pending_of: str | None = None,
    currency: str = "USD",
) -> Transaction:
    """A transaction with sensible defaults; posted one day after the purchase unless pending."""
    return Transaction(
        txn_id=txn_id,
        feed=feed,
        account_key=account,
        amount=Decimal(amount),
        spend_date=spend,
        posted_date=None if pending else (posted or spend + timedelta(days=1)),
        pending=pending,
        merchant=merchant,
        provider_primary=primary,
        provider_detailed=detailed,
        pending_of=pending_of,
        currency=currency,
    )


# 8:00 AM CDT on 14 October 2026.
MORNING = datetime(2026, 10, 14, 13, 0, tzinfo=UTC)


def budget_service(
    transactions: list[Transaction] | None = None,
    now: datetime = MORNING,
    ledger: MemoryLedger | None = None,
    synced: bool = True,
) -> BudgetService:
    ledger = ledger or MemoryLedger()
    ledger.upsert(transactions or [])
    if synced:
        stamp = now.astimezone(UTC).isoformat()
        ledger.save_status(FeedStatus("plaid:item1", stamp, None, (CHASE, ROBINHOOD)))
    return BudgetService(ledger, ledger, lambda: now)


class FixedResponder:
    """A Responder that records what it was asked and returns a fixed reply."""

    def __init__(self, reply: str = "Here is your answer.") -> None:
        self.reply = reply
        self.calls: list[tuple[str, str]] = []

    def answer(self, text: str, user_id: str, request_id: str = "") -> str:
        self.calls.append((text, user_id))
        return self.reply


class ScriptedLlm:
    """An LlmClient that plays back prepared replies and records what it was sent."""

    def __init__(self, replies: list[Any]) -> None:
        self.replies = list(replies)
        self.calls: list[dict[str, Any]] = []

    def converse(
        self,
        system: str,
        messages: list[dict[str, Any]],
        tools: list[dict[str, Any]],
        max_tokens: int,
    ) -> Any:
        import copy

        self.calls.append(
            {
                "system": system,
                "messages": copy.deepcopy(messages),
                "tools": tools,
                "max_tokens": max_tokens,
            }
        )
        reply = self.replies.pop(0)
        if isinstance(reply, Exception):
            raise reply
        return reply


def text_reply(text: str) -> Any:
    from budget_bot.llm import LlmReply

    return LlmReply(text, (), {"role": "assistant", "content": [{"text": text}]}, "test-model")


def tool_reply(*calls: tuple[str, dict[str, Any]]) -> Any:
    from budget_bot.llm import LlmReply, ToolCall

    parsed = tuple(ToolCall(f"use{i}", name, args) for i, (name, args) in enumerate(calls))
    message = {
        "role": "assistant",
        "content": [
            {"toolUse": {"toolUseId": c.tool_use_id, "name": c.name, "input": c.arguments}}
            for c in parsed
        ],
    }
    return LlmReply("", parsed, message, "test-model")


class CountingLimit:
    def __init__(self, allowed: int) -> None:
        self.allowed = allowed
        self.days: list[str] = []

    def take(self, day: str, limit: int) -> bool:
        self.days.append(day)
        self.allowed -= 1
        return self.allowed >= 0
