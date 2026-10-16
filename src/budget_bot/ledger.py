"""DynamoDB ledger: one table, a month index for transactions, and small control records.

Key layout (partition key `pk`, sort key `sk`; the `by-month` index uses `gsi1pk`/`gsi1sk`):

- transaction: pk `TXN#<id>`, sk `TXN`; index `MONTH#YYYY-MM` / `<spend date>#<id>`
- override:    pk `OVERRIDE`, sk `<txn id>`            (one tiny partition holds every correction)
- audit entry: pk `AUDIT`,    sk `<UTC time>#<random>`  (append-only)
- write mark:  pk `WRITE`,    sk `<interaction ID>`     (so a retried request does not repeat a change)
- budget:      pk `BUDGET`,   sk `YYYY-MM`              (that month's changed limits, the changes that made
                                                         them, and a revision; never deleted)
- adjustment:  pk `ADJUST`,   sk `<txn id>`            (what one charge counts for, with its history)
- rules:       pk `RULES`,    sk `ALL`                 (the household's merchant rules, with history)
- standing:    pk `DEFAULTS`, sk `LIMITS`              (standing limit changes, dated, with history)
- cursor:      pk `CURSOR`,   sk `<feed>`
- status:      pk `STATUS`,   sk `<feed>`
- lock:        pk `LOCK`,     sk `<name>`               (expires on its own)
"""

import logging
import time
from collections.abc import Callable, Iterator, Sequence
from datetime import UTC, date, datetime, timedelta
from functools import lru_cache
from typing import Any
from uuid import uuid4

import boto3
from botocore.exceptions import ClientError

from budget_bot.categories import Category
from budget_bot.ports import BudgetConflict, BudgetRecord, FeedStatus, SettingRecord
from budget_bot.transactions import AuditEntry, Override, Transaction

logger = logging.getLogger(__name__)
INDEX = "by-month"
# The kinds of household setting the worker may write (see infrastructure/stack.py).
SETTING_KINDS = frozenset({"ADJUST", "RULES", "DEFAULTS", "SPLIT"})
# Plaid supplies at most 24 months of history; keep a little longer, then let DynamoDB expire it.
RETENTION_DAYS = 800


@lru_cache(maxsize=1)
def ledger_table(name: str) -> Any:
    return boto3.resource("dynamodb").Table(name)


def _months(start: date, end: date) -> Iterator[tuple[int, int]]:
    year, month = start.year, start.month
    while (year, month) <= (end.year, end.month):
        yield year, month
        year, month = (year + 1, 1) if month == 12 else (year, month + 1)


class DynamoLedger:
    def __init__(self, table: Any, clock: Callable[[], float] = time.time) -> None:
        self.table = table
        self.clock = clock

    def _query(self, **kwargs: Any) -> list[dict[str, Any]]:
        items: list[dict[str, Any]] = []
        while True:
            page = self.table.query(**kwargs)
            items.extend(page["Items"])
            if "LastEvaluatedKey" not in page:
                return items
            kwargs["ExclusiveStartKey"] = page["LastEvaluatedKey"]

    # Transactions

    def upsert(self, transactions: Sequence[Transaction]) -> None:
        with self.table.batch_writer(overwrite_by_pkeys=["pk", "sk"]) as batch:
            for txn in transactions:
                expires = datetime.combine(
                    txn.spend_date + timedelta(days=RETENTION_DAYS), datetime.min.time(), tzinfo=UTC
                )
                batch.put_item(
                    Item={
                        "pk": f"TXN#{txn.txn_id}",
                        "sk": "TXN",
                        "gsi1pk": f"MONTH#{txn.spend_date:%Y-%m}",
                        "gsi1sk": f"{txn.spend_date.isoformat()}#{txn.txn_id}",
                        # DynamoDB deletes the record some time after this moment.
                        "ttl": int(expires.timestamp()),
                        **txn.to_item(),
                    }
                )

    def remove(self, txn_ids: Sequence[str]) -> None:
        with self.table.batch_writer(overwrite_by_pkeys=["pk", "sk"]) as batch:
            for txn_id in txn_ids:
                batch.delete_item(Key={"pk": f"TXN#{txn_id}", "sk": "TXN"})

    def between(self, start: date, end: date) -> list[Transaction]:
        found: list[Transaction] = []
        for year, month in _months(start, end):
            items = self._query(
                IndexName=INDEX,
                KeyConditionExpression="gsi1pk = :month AND gsi1sk BETWEEN :low AND :high",
                ExpressionAttributeValues={
                    ":month": f"MONTH#{year:04d}-{month:02d}",
                    ":low": start.isoformat(),
                    # "~" sorts after every character allowed in a transaction ID.
                    ":high": f"{end.isoformat()}~",
                },
            )
            found.extend(Transaction.from_item(item) for item in items)
        return found

    def get(self, txn_id: str) -> Transaction | None:
        item = self.table.get_item(Key={"pk": f"TXN#{txn_id}", "sk": "TXN"}).get("Item")
        return Transaction.from_item(item) if item else None

    def purge_feed(self, feed: str) -> int:
        """Delete every transaction from one feed, plus its cursor and freshness record.

        Used to remove sandbox rehearsal data, or a card connection before re-linking it so the
        new connection's copy of the same purchases is not counted on top of the old one.
        """
        if not feed or ":" not in feed:
            raise ValueError("feed looks like plaid:<item id> or csv:<account key>")
        removed = 0
        scan: dict[str, Any] = {
            "FilterExpression": "begins_with(pk, :txn) AND feed = :feed",
            "ExpressionAttributeValues": {":txn": "TXN#", ":feed": feed},
        }
        while True:
            page = self.table.scan(**scan)
            with self.table.batch_writer() as batch:
                for item in page["Items"]:
                    batch.delete_item(Key={"pk": item["pk"], "sk": item["sk"]})
                    removed += 1
            if "LastEvaluatedKey" not in page:
                break
            scan["ExclusiveStartKey"] = page["LastEvaluatedKey"]
        for kind in ("CURSOR", "STATUS"):
            self.table.delete_item(Key={"pk": kind, "sk": feed})
        return removed

    # Corrections

    def overrides(self) -> dict[str, Override]:
        items = self._query(
            KeyConditionExpression="pk = :pk", ExpressionAttributeValues={":pk": "OVERRIDE"}
        )
        return {item["sk"]: self._override_from(item) for item in items}

    @staticmethod
    def _override_from(item: dict[str, Any]) -> Override:
        raw = item.get("history")
        history = tuple(str(value) for value in raw) if isinstance(raw, list) else ()
        return Override(
            txn_id=item["sk"],
            category=Category(item["category"]),
            set_by=item["set_by"],
            set_at=item["set_at"],
            history=history,
            revision=int(item.get("rev", 0)),
        )

    def _write_audit(self, audit: AuditEntry) -> None:
        self.table.put_item(
            Item={
                "pk": "AUDIT",
                "sk": f"{audit.at}#{uuid4().hex[:8]}",
                "at": audit.at,
                "user_id": audit.user_id,
                "action": audit.action,
                "txn_id": audit.txn_id,
                "from_category": audit.from_category,
                "to_category": audit.to_category,
            }
        )

    def set_override(self, override: Override, audit: AuditEntry) -> None:
        # Audit first: a crash can leave an unexplained audit line but never an unaudited change.
        self._write_audit(audit)
        expected = override.revision
        item = {
            "pk": "OVERRIDE",
            "sk": override.txn_id,
            "category": override.category.value,
            "set_by": override.set_by,
            "set_at": override.set_at,
            "history": list(override.history or (override.category.value,)),
            "rev": expected + 1,
        }
        condition: dict[str, Any] = (
            {
                "ConditionExpression": "attribute_not_exists(pk) OR attribute_not_exists(rev)",
            }
            if expected == 0
            else {
                "ConditionExpression": "rev = :expected",
                "ExpressionAttributeValues": {":expected": expected},
            }
        )
        try:
            self.table.put_item(Item=item, **condition)
        except ClientError as exc:
            if exc.response["Error"]["Code"] == "ConditionalCheckFailedException":
                raise BudgetConflict(override.txn_id) from exc
            raise

    def clear_override(
        self, txn_id: str, audit: AuditEntry, expected_revision: int | None = None
    ) -> None:
        self._write_audit(audit)
        condition: dict[str, Any] = {}
        if expected_revision is not None:
            condition = (
                {"ConditionExpression": "attribute_not_exists(rev) OR rev = :expected"}
                if expected_revision == 0
                else {
                    "ConditionExpression": "rev = :expected",
                    "ExpressionAttributeValues": {":expected": expected_revision},
                }
            )
        try:
            self.table.delete_item(Key={"pk": "OVERRIDE", "sk": txn_id}, **condition)
        except ClientError as exc:
            if exc.response["Error"]["Code"] == "ConditionalCheckFailedException":
                raise BudgetConflict(txn_id) from exc
            raise

    def audit_log(self, limit: int) -> list[AuditEntry]:
        page = self.table.query(
            KeyConditionExpression="pk = :pk",
            ExpressionAttributeValues={":pk": "AUDIT"},
            ScanIndexForward=False,
            Limit=limit,
            ConsistentRead=True,  # an undo right after a change must see that change
        )
        return [
            AuditEntry(
                at=item["at"],
                user_id=item["user_id"],
                action=item["action"],
                txn_id=item["txn_id"],
                from_category=item["from_category"],
                to_category=item["to_category"],
            )
            for item in page["Items"]
        ]

    def recall_write(self, key: str) -> str | None:
        item = self.table.get_item(Key={"pk": "WRITE", "sk": key}, ConsistentRead=True)
        result = item.get("Item", {}).get("result")
        return result if isinstance(result, str) else None

    def record_write(self, key: str, result: str) -> None:
        self.table.put_item(
            Item={
                "pk": "WRITE",
                "sk": key,
                "result": result,
                "ttl": int(self.clock()) + 2 * 86400,
            }
        )

    # Monthly budget changes

    def budget_record(self, month: str) -> BudgetRecord:
        item = self.table.get_item(Key={"pk": "BUDGET", "sk": month}, ConsistentRead=True).get(
            "Item"
        )
        if not item:
            return BudgetRecord()
        limits, history = item.get("limits"), item.get("history")
        return BudgetRecord(
            {str(k): str(v) for k, v in limits.items()} if isinstance(limits, dict) else {},
            tuple(entry for entry in history if isinstance(entry, dict))
            if isinstance(history, list)
            else (),
            int(item.get("rev", 0)),
        )

    def save_budget_record(
        self,
        month: str,
        limits: dict[str, str],
        history: Sequence[dict[str, Any]],
        expected_revision: int,
        audit: AuditEntry,
    ) -> None:
        # One conditional write replaces the month's limits and history together, so two people
        # changing the budget at once cannot lose each other's change, and the history an undo
        # relies on can never disagree with the limits. A record is never deleted, so a stale
        # writer can never recreate one.
        item: dict[str, Any] = {
            "pk": "BUDGET",
            "sk": month,
            "limits": dict(limits),
            "history": [dict(entry) for entry in history],
            "rev": expected_revision + 1,
        }
        self._put_versioned(item, expected_revision, month, audit)

    def _put_versioned(
        self, item: dict[str, Any], expected_revision: int, label: str, audit: AuditEntry
    ) -> None:
        """Write a record only if it is still at the revision the caller read, then log it."""
        condition: dict[str, Any] = (
            {"ConditionExpression": "attribute_not_exists(pk)"}
            if expected_revision == 0
            else {
                "ConditionExpression": "rev = :expected",
                "ExpressionAttributeValues": {":expected": expected_revision},
            }
        )
        try:
            self.table.put_item(Item=item, **condition)
        except ClientError as exc:
            if exc.response["Error"]["Code"] == "ConditionalCheckFailedException":
                raise BudgetConflict(label) from exc
            raise
        try:
            self._write_audit(audit)
        except Exception:
            # The record's own history already says who changed what and when; a failed log
            # line must not turn a change that happened into an error.
            logger.warning("setting_audit_write_failed")

    # Household settings: charge adjustments, merchant rules, standing limits

    @staticmethod
    def _setting_record(item: dict[str, Any]) -> SettingRecord:
        data = item.get("data")
        return SettingRecord(data if isinstance(data, dict) else {}, int(item.get("rev", 0)))

    def setting(self, kind: str, key: str) -> SettingRecord:
        self._check_kind(kind)
        item = self.table.get_item(Key={"pk": kind, "sk": key}, ConsistentRead=True).get("Item")
        return self._setting_record(item) if item else SettingRecord()

    def settings_of(self, kind: str) -> dict[str, SettingRecord]:
        self._check_kind(kind)
        items = self._query(
            KeyConditionExpression="pk = :pk",
            ExpressionAttributeValues={":pk": kind},
            ConsistentRead=True,
        )
        return {item["sk"]: self._setting_record(item) for item in items}

    def save_setting(
        self,
        kind: str,
        key: str,
        data: dict[str, Any],
        expected_revision: int,
        audit: AuditEntry,
    ) -> None:
        self._check_kind(kind)
        item = {"pk": kind, "sk": key, "data": data, "rev": expected_revision + 1}
        self._put_versioned(item, expected_revision, f"{kind}/{key}", audit)

    @staticmethod
    def _check_kind(kind: str) -> None:
        if kind not in SETTING_KINDS:
            raise ValueError("unknown setting kind")

    # Sync control records

    def cursor(self, feed: str) -> str | None:
        item = self.table.get_item(Key={"pk": "CURSOR", "sk": feed}, ConsistentRead=True)
        return item.get("Item", {}).get("cursor")

    def save_cursor(self, feed: str, cursor: str) -> None:
        self.table.put_item(Item={"pk": "CURSOR", "sk": feed, "cursor": cursor})

    def statuses(self) -> list[FeedStatus]:
        items = self._query(
            KeyConditionExpression="pk = :pk", ExpressionAttributeValues={":pk": "STATUS"}
        )
        return [
            FeedStatus(
                feed=item["sk"],
                last_success=item.get("last_success"),
                last_error=item.get("last_error"),
                accounts=tuple(item.get("accounts", ())),
            )
            for item in items
        ]

    def save_status(self, status: FeedStatus) -> None:
        item: dict[str, Any] = {
            "pk": "STATUS",
            "sk": status.feed,
            "accounts": list(status.accounts),
        }
        if status.last_success:
            item["last_success"] = status.last_success
        if status.last_error:
            item["last_error"] = status.last_error
        self.table.put_item(Item=item)

    def lock(self, name: str, owner: str, seconds: int) -> bool:
        now = int(self.clock())
        try:
            self.table.put_item(
                Item={
                    "pk": "LOCK",
                    "sk": name,
                    "owner": owner,
                    "lock_until": now + seconds,
                    "ttl": now + seconds + 3600,
                },
                ConditionExpression="attribute_not_exists(pk) OR lock_until <= :now",
                ExpressionAttributeValues={":now": now},
            )
            return True
        except ClientError as exc:
            if exc.response["Error"]["Code"] != "ConditionalCheckFailedException":
                raise
            return False

    def unlock(self, name: str, owner: str) -> None:
        try:
            self.table.delete_item(
                Key={"pk": "LOCK", "sk": name},
                ConditionExpression="#owner = :owner",
                ExpressionAttributeNames={"#owner": "owner"},
                ExpressionAttributeValues={":owner": owner},
            )
        except ClientError as exc:
            if exc.response["Error"]["Code"] != "ConditionalCheckFailedException":
                raise
