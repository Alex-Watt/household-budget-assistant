"""Small contracts at the application's external boundaries."""

from collections.abc import Sequence
from dataclasses import dataclass, field
from datetime import date
from typing import Any, Protocol

from budget_bot.transactions import AuditEntry, Override, Transaction


class ReplySender(Protocol):
    def edit_reply(self, token: str, text: str) -> None: ...


class ChannelPoster(Protocol):
    def post(self, text: str) -> None: ...


class MessageInProgress(RuntimeError):
    """Another worker holds the lease; SQS must retry rather than acknowledge."""


class MessagingState(Protocol):
    def claim(self, message_id: str) -> str | None:
        """Return an ownership token, None if complete, or raise MessageInProgress."""
        ...

    def complete(self, message_id: str, token: str) -> None: ...

    def release(self, message_id: str, token: str) -> None: ...


class DailyLimit(Protocol):
    def take(self, day: str, limit: int) -> bool:
        """Count one use for `day` (YYYY-MM-DD) and return False once `limit` is reached."""
        ...


class Responder(Protocol):
    """Turns a household question into the reply text posted to the channel."""

    def answer(self, text: str, user_id: str, request_id: str = "") -> str: ...


class Ledger(Protocol):
    """Normalized transactions plus the household's reversible corrections."""

    def upsert(self, transactions: Sequence[Transaction]) -> None: ...

    def remove(self, txn_ids: Sequence[str]) -> None: ...

    def between(self, start: date, end: date) -> list[Transaction]:
        """Transactions whose spend date falls in the inclusive range."""
        ...

    def get(self, txn_id: str) -> Transaction | None: ...

    def overrides(self) -> dict[str, Override]: ...

    def set_override(self, override: Override, audit: AuditEntry) -> None: ...

    def clear_override(
        self, txn_id: str, audit: AuditEntry, expected_revision: int | None = None
    ) -> None: ...

    def audit_log(self, limit: int) -> list[AuditEntry]: ...

    def recall_write(self, key: str) -> str | None:
        """The stored result of a change already made for this request, if any."""
        ...

    def record_write(self, key: str, result: str) -> None: ...

    def budget_record(self, month: str) -> BudgetRecord:
        """The household's changed category limits for one month ("2026-10")."""
        ...

    def save_budget_record(
        self,
        month: str,
        limits: dict[str, str],
        history: Sequence[dict[str, Any]],
        expected_revision: int,
        audit: AuditEntry,
    ) -> None:
        """Store a month's limits and history if the month is still at `expected_revision`.

        Raises BudgetConflict if someone else changed it first, so a change is never lost.
        """
        ...

    def setting(self, kind: str, key: str) -> SettingRecord:
        """One household setting: `kind` is ADJUST, RULES or DEFAULTS."""
        ...

    def settings_of(self, kind: str) -> dict[str, SettingRecord]:
        """Every setting of one kind, by key (the charge adjustments, for example)."""
        ...

    def save_setting(
        self,
        kind: str,
        key: str,
        data: dict[str, Any],
        expected_revision: int,
        audit: AuditEntry,
    ) -> None:
        """Store a setting if it is still at `expected_revision`, else raise BudgetConflict."""
        ...


@dataclass(frozen=True)
class SettingRecord:
    """A small household setting (charge adjustments, merchant rules, standing limits).

    `data` holds only text, lists and maps of text. `revision` increases with every save.
    """

    data: dict[str, Any] = field(default_factory=dict)
    revision: int = 0


@dataclass(frozen=True)
class BudgetRecord:
    """One month's changed limits (category id to dollars as text; absent means the default),
    the changes that produced them (newest last, so an undo is just the last entry), and a
    revision that increases with every save."""

    limits: dict[str, str] = field(default_factory=dict)
    history: tuple[dict[str, Any], ...] = ()
    revision: int = 0


class BudgetConflict(RuntimeError):
    """A setting (a month's budget, a rule, an adjustment) was changed by someone else between
    reading and saving."""


@dataclass(frozen=True)
class FeedStatus:
    feed: str
    last_success: str | None  # ISO-8601 UTC
    last_error: str | None  # a provider error code such as ITEM_LOGIN_REQUIRED
    accounts: tuple[str, ...]  # account keys the feed supplies


class SyncState(Protocol):
    """Cursors, freshness and per-feed locks for incremental syncs."""

    def cursor(self, feed: str) -> str | None: ...

    def save_cursor(self, feed: str, cursor: str) -> None: ...

    def statuses(self) -> list[FeedStatus]: ...

    def save_status(self, status: FeedStatus) -> None: ...

    def lock(self, name: str, owner: str, seconds: int) -> bool: ...

    def unlock(self, name: str, owner: str) -> None: ...


@dataclass(frozen=True)
class SyncBatch:
    upserts: tuple[Transaction, ...]  # added and modified records, already normalized
    removed: tuple[str, ...]  # transaction IDs the provider deleted
    cursor: str | None
    accounts: tuple[str, ...]


class ProviderError(RuntimeError):
    """A transaction provider refused or failed. `code` is safe to store and show."""

    def __init__(self, code: str, retryable: bool = True) -> None:
        super().__init__(code)
        self.code = code
        self.retryable = retryable


class TransactionProvider(Protocol):
    """Read-only source of transactions; implementations never move money."""

    def feeds(self) -> list[str]: ...

    def fetch(self, feed: str, cursor: str | None) -> SyncBatch: ...
