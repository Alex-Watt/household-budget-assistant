"""DynamoDB adapter for processing leases on questions and daily reports."""

import time
from collections.abc import Callable
from functools import lru_cache
from typing import Any
from uuid import uuid4

import boto3
from botocore.exceptions import ClientError

from budget_bot.ports import MessageInProgress

# Longer than the 90-second worker timeout; shorter than the 540-second queue visibility.
LEASE_SECONDS = 120
RETENTION_SECONDS = 90 * 86400


@lru_cache(maxsize=1)
def state_table(name: str) -> Any:
    return boto3.resource("dynamodb").Table(name)


class DynamoMessagingState:
    def __init__(self, table: Any, clock: Callable[[], float] = time.time) -> None:
        self.table = table
        self.clock = clock

    def claim(self, message_id: str) -> str | None:
        now = int(self.clock())
        token = uuid4().hex
        try:
            self.table.put_item(
                Item={
                    "pk": f"MSG#{message_id}",
                    "status": "processing",
                    "token": token,
                    "lease_until": now + LEASE_SECONDS,
                    "ttl": now + RETENTION_SECONDS,
                },
                ConditionExpression=(
                    "attribute_not_exists(pk) OR #ttl <= :now OR "
                    "(#status = :processing AND lease_until <= :now)"
                ),
                ExpressionAttributeNames={"#status": "status", "#ttl": "ttl"},
                ExpressionAttributeValues={":now": now, ":processing": "processing"},
            )
            return token
        except ClientError as exc:
            if exc.response["Error"]["Code"] != "ConditionalCheckFailedException":
                raise
        item = self.table.get_item(Key={"pk": f"MSG#{message_id}"}, ConsistentRead=True).get(
            "Item", {}
        )
        # Legacy records have no status and represent completion under the original prototype.
        if item and item.get("status", "complete") == "complete" and item.get("ttl", 0) > now:
            return None
        raise MessageInProgress("message processing lease is busy; retry later")

    def complete(self, message_id: str, token: str) -> None:
        self.table.update_item(
            Key={"pk": f"MSG#{message_id}"},
            UpdateExpression="SET #status = :complete REMOVE lease_until",
            ConditionExpression="#token = :token AND #status = :processing",
            ExpressionAttributeNames={"#status": "status", "#token": "token"},
            ExpressionAttributeValues={
                ":complete": "complete",
                ":processing": "processing",
                ":token": token,
            },
        )

    def release(self, message_id: str, token: str) -> None:
        try:
            self.table.delete_item(
                Key={"pk": f"MSG#{message_id}"},
                ConditionExpression="#token = :token AND #status = :processing",
                ExpressionAttributeNames={"#token": "token", "#status": "status"},
                ExpressionAttributeValues={":token": token, ":processing": "processing"},
            )
        except ClientError as exc:
            if exc.response["Error"]["Code"] != "ConditionalCheckFailedException":
                raise


class DynamoDailyLimit:
    """A per-day counter, e.g. for AI answers. Stored beside the message leases."""

    def __init__(self, table: Any, clock: Callable[[], float] = time.time) -> None:
        self.table = table
        self.clock = clock

    def take(self, day: str, limit: int) -> bool:
        try:
            self.table.update_item(
                Key={"pk": f"AILIMIT#{day}"},
                UpdateExpression="ADD uses :one SET #ttl = :ttl",
                ConditionExpression="attribute_not_exists(uses) OR uses < :limit",
                ExpressionAttributeNames={"#ttl": "ttl"},
                ExpressionAttributeValues={
                    ":one": 1,
                    ":limit": limit,
                    ":ttl": int(self.clock()) + 3 * 86400,
                },
            )
            return True
        except ClientError as exc:
            if exc.response["Error"]["Code"] != "ConditionalCheckFailedException":
                raise
            return False

    def add_tokens(self, day: str, tokens: int, cap: int) -> bool:
        """Reserve `tokens` of today's model input and return False once `cap` is reached."""
        if tokens <= 0:
            return True
        try:
            self.table.update_item(
                Key={"pk": f"AITOKENS#{day}"},
                UpdateExpression="ADD tokens :n SET #ttl = :ttl",
                ConditionExpression="attribute_not_exists(tokens) OR tokens < :cap",
                ExpressionAttributeNames={"#ttl": "ttl"},
                ExpressionAttributeValues={
                    ":n": tokens,
                    ":cap": cap,
                    ":ttl": int(self.clock()) + 3 * 86400,
                },
            )
            return True
        except ClientError as exc:
            if exc.response["Error"]["Code"] != "ConditionalCheckFailedException":
                raise
            return False

    def recall_context(self) -> dict[str, Any] | None:
        item = self.table.get_item(Key={"pk": "CONTEXT", "sk": "LAST"}, ConsistentRead=True).get(
            "Item"
        )
        data = item.get("data") if item else None
        return data if isinstance(data, dict) else None

    def save_context(self, data: dict[str, Any]) -> None:
        self.table.put_item(
            Item={
                "pk": "CONTEXT",
                "sk": "LAST",
                "data": data,
                "ttl": int(self.clock()) + 7 * 86400,
            }
        )
