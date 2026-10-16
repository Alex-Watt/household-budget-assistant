"""AWS secret access, kept separate from configuration and message rules."""

import json
import re
import time
from dataclasses import dataclass, field
from functools import lru_cache
from typing import Any

import boto3

from budget_bot.accounts import validate_account_key
from budget_bot.discord import WEBHOOK_URL

PLAID_ENVIRONMENTS = frozenset({"sandbox", "production"})
ITEM_ID = re.compile(r"[A-Za-z0-9_\-]{1,100}\Z")
# Short, so a card linked a moment ago is recognized quickly; the sync Lambda reads it fresh.
PLAID_CACHE_SECONDS = 60


@lru_cache(maxsize=1)
def report_webhook_url(secret_name: str) -> str:
    response = boto3.client("secretsmanager").get_secret_value(SecretId=secret_name)
    value = response.get("SecretString")
    if not isinstance(value, str) or not WEBHOOK_URL.fullmatch(value.strip()):
        raise ValueError("Discord secret must be a discord.com channel webhook URL")
    return value.strip()


@dataclass(frozen=True)
class PlaidItem:
    """One bank login. Its access token reads transactions only; it can never move money."""

    item_id: str
    access_token: str = field(repr=False)
    institution: str
    # Plaid account ID -> the household's key for that card. Unlabeled accounts are ignored.
    accounts: dict[str, str]


@dataclass(frozen=True)
class PlaidConfig:
    client_id: str
    secret: str = field(repr=False)
    environment: str
    items: tuple[PlaidItem, ...]

    def item(self, item_id: str) -> PlaidItem | None:
        return next((item for item in self.items if item.item_id == item_id), None)


def parse_plaid_config(text: str) -> PlaidConfig:
    """Validate the secret's JSON. Error messages never include secret values."""
    try:
        raw: Any = json.loads(text)
        items = tuple(
            PlaidItem(
                item_id=item["item_id"],
                access_token=item["access_token"],
                institution=str(item.get("institution", "")),
                accounts={
                    account_id: validate_account_key(key)
                    for account_id, key in item["accounts"].items()
                },
            )
            for item in raw.get("items", [])
        )
        config = PlaidConfig(raw["client_id"], raw["secret"], raw["environment"], items)
    except (KeyError, TypeError, AttributeError, ValueError) as exc:
        raise ValueError("Plaid secret is not valid JSON of the expected shape") from exc
    ids = [item.item_id for item in items]
    if (
        not config.client_id
        or not config.secret
        or config.environment not in PLAID_ENVIRONMENTS
        or len(set(ids)) != len(ids)
        or not all(ITEM_ID.fullmatch(i) for i in ids)
        or not all(item.access_token.startswith("access-") for item in items)
    ):
        raise ValueError("Plaid secret has an invalid client, environment or item")
    return config


_plaid_cache: dict[str, tuple[float, PlaidConfig]] = {}


def plaid_config(secret_name: str, now: float | None = None, fresh: bool = False) -> PlaidConfig:
    """Read the Plaid secret, cached for a minute unless `fresh` asks for the latest copy."""
    moment = time.monotonic() if now is None else now
    cached = _plaid_cache.get(secret_name)
    if cached and not fresh and moment - cached[0] < PLAID_CACHE_SECONDS:
        return cached[1]
    response = boto3.client("secretsmanager").get_secret_value(SecretId=secret_name)
    value = response.get("SecretString")
    if not isinstance(value, str):
        raise ValueError("Plaid secret must be a JSON string")
    config = parse_plaid_config(value)
    _plaid_cache[secret_name] = (moment, config)
    return config
