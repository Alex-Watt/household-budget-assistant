"""In-memory ledger for tests, fixtures and the local demo. Nothing here touches AWS."""

import copy
import time
from collections.abc import Callable, Sequence
from datetime import date
from typing import Any

from budget_bot.ports import BudgetConflict, BudgetRecord, FeedStatus, SettingRecord
from budget_bot.transactions import AuditEntry, Override, Transaction


class MemoryLedger:
    def __init__(self, clock: Callable[[], float] = time.time) -> None:
        self.clock = clock
        self.transactions: dict[str, Transaction] = {}
        self._overrides: dict[str, Override] = {}
        self._audit: list[AuditEntry] = []
        self._writes: dict[str, str] = {}
        self._budgets: dict[str, BudgetRecord] = {}
        self._settings: dict[tuple[str, str], SettingRecord] = {}
        self._cursors: dict[str, str] = {}
        self._statuses: dict[str, FeedStatus] = {}
        self._locks: dict[str, tuple[str, float]] = {}

    def upsert(self, transactions: Sequence[Transaction]) -> None:
        for txn in transactions:
            self.transactions[txn.txn_id] = txn

    def remove(self, txn_ids: Sequence[str]) -> None:
        for txn_id in txn_ids:
            self.transactions.pop(txn_id, None)

    def between(self, start: date, end: date) -> list[Transaction]:
        return [t for t in self.transactions.values() if start <= t.spend_date <= end]

    def get(self, txn_id: str) -> Transaction | None:
        return self.transactions.get(txn_id)

    def overrides(self) -> dict[str, Override]:
        return dict(self._overrides)

    def set_override(self, override: Override, audit: AuditEntry) -> None:
        current = self._overrides.get(override.txn_id)
        if (current.revision if current else 0) != override.revision:
            raise BudgetConflict(override.txn_id)
        self._audit.append(audit)
        self._overrides[override.txn_id] = Override(
            override.txn_id,
            override.category,
            override.set_by,
            override.set_at,
            override.history or (override.category.value,),
            override.revision + 1,
        )

    def clear_override(
        self, txn_id: str, audit: AuditEntry, expected_revision: int | None = None
    ) -> None:
        current = self._overrides.get(txn_id)
        if expected_revision is not None and (
            current is None or current.revision != expected_revision
        ):
            raise BudgetConflict(txn_id)
        self._audit.append(audit)
        self._overrides.pop(txn_id, None)

    def audit_log(self, limit: int) -> list[AuditEntry]:
        return list(reversed(self._audit))[:limit]

    def recall_write(self, key: str) -> str | None:
        return self._writes.get(key)

    def record_write(self, key: str, result: str) -> None:
        self._writes[key] = result

    def budget_record(self, month: str) -> BudgetRecord:
        return self._budgets.get(month, BudgetRecord())

    def setting(self, kind: str, key: str) -> SettingRecord:
        return self._settings.get((kind, key), SettingRecord())

    def settings_of(self, kind: str) -> dict[str, SettingRecord]:
        return {key: record for (k, key), record in self._settings.items() if k == kind}

    def save_setting(
        self,
        kind: str,
        key: str,
        data: dict[str, Any],
        expected_revision: int,
        audit: AuditEntry,
    ) -> None:
        if self.setting(kind, key).revision != expected_revision:
            raise BudgetConflict(f"{kind}/{key}")
        self._settings[(kind, key)] = SettingRecord(copy.deepcopy(data), expected_revision + 1)
        self._audit.append(audit)

    def save_budget_record(
        self,
        month: str,
        limits: dict[str, str],
        history: Sequence[dict[str, Any]],
        expected_revision: int,
        audit: AuditEntry,
    ) -> None:
        if self.budget_record(month).revision != expected_revision:
            raise BudgetConflict(month)
        self._budgets[month] = BudgetRecord(
            dict(limits), tuple(dict(entry) for entry in history), expected_revision + 1
        )
        self._audit.append(audit)

    def cursor(self, feed: str) -> str | None:
        return self._cursors.get(feed)

    def save_cursor(self, feed: str, cursor: str) -> None:
        self._cursors[feed] = cursor

    def statuses(self) -> list[FeedStatus]:
        return [self._statuses[feed] for feed in sorted(self._statuses)]

    def save_status(self, status: FeedStatus) -> None:
        self._statuses[status.feed] = status

    def lock(self, name: str, owner: str, seconds: int) -> bool:
        now = self.clock()
        held = self._locks.get(name)
        if held and held[1] > now:
            return False
        self._locks[name] = (owner, now + seconds)
        return True

    def unlock(self, name: str, owner: str) -> None:
        held = self._locks.get(name)
        if held and held[0] == owner:
            del self._locks[name]
