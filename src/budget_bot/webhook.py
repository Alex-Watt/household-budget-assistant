import base64
import json
import os
from functools import lru_cache
from typing import Any

import boto3

from budget_bot.config import Settings
from budget_bot.discord import (
    ASK_COMMAND,
    Command,
    Ping,
    RejectedRequest,
    deferred,
    parse_interaction,
    pong,
    private_message,
    verify_request,
)
from budget_bot.models import MAX_QUESTION, Question

NOT_HERE = "This bot only answers the household members in the budget channel."
DRY_RUN = "Got your question. The budget bot is in dry-run mode, so replies are turned off."


@lru_cache(maxsize=1)
def _sqs() -> Any:
    return boto3.client("sqs")


def _json(body: dict[str, Any]) -> dict[str, Any]:
    return {
        "statusCode": 200,
        "headers": {"Content-Type": "application/json"},
        "body": json.dumps(body),
    }


def _is_household(command: Command, settings: Settings) -> bool:
    return (
        command.name == ASK_COMMAND
        and command.application_id == settings.application_id
        and command.guild_id == settings.guild_id
        and command.channel_id == settings.channel_id
        and command.user_id in settings.member_ids
    )


def handle(
    event: dict[str, Any], queue: Any, settings: Settings, now: int | None = None
) -> dict[str, Any]:
    if event.get("requestContext", {}).get("http", {}).get("method") != "POST":
        return {"statusCode": 405, "body": "Method Not Allowed"}
    try:
        body = event.get("body", "")
        if not isinstance(body, str):
            return {"statusCode": 401, "body": "Unauthorized"}
        raw = (
            base64.b64decode(body, validate=True)
            if event.get("isBase64Encoded")
            else body.encode("utf-8")
        )
        if len(raw) > 256_000:
            return {"statusCode": 413, "body": "Payload Too Large"}
        verify_request(raw, event.get("headers") or {}, settings.public_key, now=now)
    except RejectedRequest, ValueError, TypeError, UnicodeError, AttributeError:
        return {"statusCode": 401, "body": "Unauthorized"}
    try:
        interaction = parse_interaction(raw)
        if isinstance(interaction, Ping):
            return _json(pong())
        if not _is_household(interaction, settings):
            return _json(private_message(NOT_HERE))
        question = Question(
            interaction_id=interaction.interaction_id,
            user_id=interaction.user_id,
            text=interaction.question[:MAX_QUESTION],
            token=interaction.token,
        )
    except ValueError:
        return {"statusCode": 400, "body": "Bad Request"}
    queue.send_message(QueueUrl=os.environ["QUEUE_URL"], MessageBody=json.dumps(question.to_dict()))
    return _json(deferred() if settings.live_messaging else private_message(DRY_RUN))


def lambda_handler(event: dict[str, Any], context: Any) -> dict[str, Any]:
    return handle(event, _sqs(), Settings.from_env())
