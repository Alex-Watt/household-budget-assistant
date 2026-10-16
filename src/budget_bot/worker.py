"""SQS Lambda entry point; question rules live in messaging.py and answers in assistant.py."""

import json
import logging
import os
from collections.abc import Callable
from datetime import datetime
from functools import lru_cache
from typing import Any

import boto3
from botocore.config import Config

from budget_bot.assistant import Assistant
from budget_bot.budget import HOUSEHOLD_TIME, BudgetService
from budget_bot.config import Settings
from budget_bot.discord import DiscordReplies, MockDiscord
from budget_bot.ledger import DynamoLedger, ledger_table
from budget_bot.llm import BedrockSonnet
from budget_bot.messaging import process
from budget_bot.models import Question
from budget_bot.ports import MessagingState, ReplySender, Responder
from budget_bot.state import DynamoDailyLimit, DynamoMessagingState, state_table

logger = logging.getLogger(__name__)
logger.setLevel(logging.INFO)

# One attempt, with long enough to finish a normal answer. A short timeout makes Bedrock
# generate a reply the client then abandons, and the retry pays for it again.
# The assistant also checks the real time left before every round (ROUND_RESERVE_SECONDS).
BEDROCK_CONFIG = Config(
    retries={"total_max_attempts": 1, "mode": "standard"}, connect_timeout=3, read_timeout=30
)
BEDROCK_CONTROL_CONFIG = Config(
    retries={"total_max_attempts": 2}, connect_timeout=3, read_timeout=5
)


def handle(
    event: dict[str, Any],
    state: MessagingState,
    replies: ReplySender,
    settings: Settings,
    responder: Responder,
) -> dict[str, Any]:
    failures = []
    for record in event["Records"]:
        try:
            question = Question.from_dict(json.loads(record["body"]))
            outcome = process(question, state, replies, settings, responder)
            logger.info("message_outcome=%s", outcome)
        except Exception as exc:
            # Do not log questions, user IDs, interaction tokens or provider exception messages.
            logger.warning("message_failed error_type=%s", type(exc).__name__)
            failures.append({"itemIdentifier": record["messageId"]})
    return {"batchItemFailures": failures}


@lru_cache(maxsize=1)
def _model(scope: str) -> BedrockSonnet:
    # Kept for the warm runtime so the list of available Sonnet versions is not re-fetched.
    return BedrockSonnet(
        boto3.client("bedrock-runtime", config=BEDROCK_CONFIG),
        boto3.client("bedrock", config=BEDROCK_CONTROL_CONFIG),
        scope,
    )


def build_responder(settings: Settings, time_left: Callable[[], float] | None = None) -> Responder:
    ledger = DynamoLedger(ledger_table(os.environ["LEDGER_TABLE"]))
    service = BudgetService(ledger, ledger, lambda: datetime.now(HOUSEHOLD_TIME))
    context = DynamoMessagingState(state_table(os.environ["STATE_TABLE"]))
    if not settings.ai_enabled:
        return Assistant(service, None, None, settings.ai_daily_limit, context=context)
    limit = DynamoDailyLimit(state_table(os.environ["STATE_TABLE"]))
    return Assistant(
        service,
        _model(settings.ai_profile_scope),
        limit,
        settings.ai_daily_limit,
        time_left,
        context,
    )


def lambda_handler(event: dict[str, Any], context: Any) -> dict[str, Any]:
    settings = Settings.from_env()
    state = DynamoMessagingState(state_table(os.environ["STATE_TABLE"]))
    replies: ReplySender = (
        DiscordReplies(settings.application_id) if settings.live_messaging else MockDiscord()
    )
    time_left = (lambda: context.get_remaining_time_in_millis() / 1000) if context else None
    return handle(event, state, replies, settings, build_responder(settings, time_left))
