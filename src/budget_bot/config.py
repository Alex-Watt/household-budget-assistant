"""Validated settings shared by CDK and the Lambda runtime; no network calls."""

import os
import re
from collections.abc import Mapping
from dataclasses import dataclass

SNOWFLAKE = re.compile(r"[1-9][0-9]{16,19}\Z")
HEX_KEY = re.compile(r"[0-9a-f]{64}\Z")
# Secrets Manager names only: no wildcards, spaces or paths that could widen an IAM grant.
SECRET_NAME = re.compile(r"[A-Za-z0-9/_+=.@-]{1,256}\Z")
EMAIL = re.compile(r"[^@\s]{1,64}@[^@\s]{1,255}\.[^@\s]{2,}\Z")

# Ed25519 keys with these y-coordinates (either sign bit) have small order and accept forgeries.
SMALL_ORDER_KEYS = frozenset(
    bytes.fromhex(value)
    for value in (
        "00" * 32,
        "01" + "00" * 31,
        "26e8958fc2b227b045c3f489f2ef98f0d5dfac05d3c63339b13802886d53fc05",
        "c7176a703d4dd84fba3c0b760d10670f2a2053fa2c39ccc64ec7fd7792ac037a",
        "ec" + "ff" * 30 + "7f",
        "ed" + "ff" * 30 + "7f",
        "ee" + "ff" * 30 + "7f",
    )
)


def _is_usable_public_key(value: str) -> bool:
    if not isinstance(value, str) or not HEX_KEY.fullmatch(value):
        return False
    raw = bytearray(bytes.fromhex(value))
    raw[31] &= 0x7F
    return bytes(raw) not in SMALL_ORDER_KEYS


def _flag(values: Mapping[str, str], name: str) -> bool:
    value = values.get(name, "false").lower()
    if value not in {"true", "false"}:
        raise ValueError(f"{name} must be true or false")
    return value == "true"


@dataclass(frozen=True)
class Settings:
    application_id: str
    # Discord's interaction verification key is public; only the report webhook URL is a secret.
    public_key: str
    guild_id: str
    channel_id: str
    member_ids: tuple[str, ...]
    live_messaging: bool = False
    secret_name: str = "household-budget/discord"
    # AI answers are off until the AWS project can call the model; commands work without them.
    ai_enabled: bool = False
    ai_daily_limit: int = 30
    # "us" keeps model traffic inside the United States; "global" may route anywhere.
    ai_profile_scope: str = "us"
    plaid_enabled: bool = False
    plaid_secret_name: str = "household-budget/plaid"
    # Optional: where CloudWatch alarms and the monthly cost budget send email.
    alert_email: str = ""
    monthly_cost_alert_usd: int = 50

    def __post_init__(self) -> None:
        ids = (self.application_id, self.guild_id, self.channel_id, *self.member_ids)
        if not 1 <= len(self.member_ids) <= 2 or not all(
            isinstance(i, str) and SNOWFLAKE.fullmatch(i) for i in ids
        ):
            raise ValueError("Set every DISCORD_*_ID value to a Discord snowflake ID")
        if len(set(self.member_ids)) != len(self.member_ids):
            raise ValueError("Set distinct DISCORD_MEMBER_*_ID values")
        if not _is_usable_public_key(self.public_key):
            raise ValueError("DISCORD_PUBLIC_KEY must be the application's 64-character hex key")
        if not all(SECRET_NAME.fullmatch(n) for n in (self.secret_name, self.plaid_secret_name)):
            raise ValueError("secret names must be plain Secrets Manager names")
        if not 1 <= self.ai_daily_limit <= 500:
            raise ValueError("AI_DAILY_LIMIT must be between 1 and 500")
        if self.ai_profile_scope not in {"us", "global"}:
            raise ValueError("AI_PROFILE_SCOPE must be us or global")
        if self.alert_email and not EMAIL.fullmatch(self.alert_email):
            raise ValueError("ALERT_EMAIL must be an email address")
        if not 1 <= self.monthly_cost_alert_usd <= 10_000:
            raise ValueError("MONTHLY_COST_ALERT_USD must be between 1 and 10000")

    @classmethod
    def from_env(cls, env: Mapping[str, str] | None = None) -> Settings:
        values = os.environ if env is None else env
        live = _flag(values, "LIVE_MESSAGING_ENABLED")
        try:
            ai_daily_limit = int(values.get("AI_DAILY_LIMIT", "30"))
            monthly_cost = int(values.get("MONTHLY_COST_ALERT_USD", "50"))
        except ValueError as exc:
            raise ValueError(
                "AI_DAILY_LIMIT and MONTHLY_COST_ALERT_USD must be whole numbers"
            ) from exc
        first = values.get("DISCORD_MEMBER_1_ID", "").strip()
        second = values.get("DISCORD_MEMBER_2_ID", "").strip()
        if not first:
            member_ids: tuple[str, ...] = ("",)
        elif second:
            member_ids = (first, second)
        else:
            member_ids = (first,)
        return cls(
            application_id=values.get("DISCORD_APPLICATION_ID", ""),
            public_key=values.get("DISCORD_PUBLIC_KEY", "").strip().lower(),
            guild_id=values.get("DISCORD_GUILD_ID", ""),
            channel_id=values.get("DISCORD_CHANNEL_ID", ""),
            member_ids=member_ids,
            live_messaging=live,
            secret_name=values.get("DISCORD_SECRET_NAME", "household-budget/discord"),
            ai_enabled=_flag(values, "AI_ENABLED"),
            ai_daily_limit=ai_daily_limit,
            ai_profile_scope=values.get("AI_PROFILE_SCOPE", "us").strip().lower(),
            plaid_enabled=_flag(values, "PLAID_ENABLED"),
            plaid_secret_name=values.get("PLAID_SECRET_NAME", "household-budget/plaid"),
            alert_email=values.get("ALERT_EMAIL", "").strip(),
            monthly_cost_alert_usd=monthly_cost,
        )
