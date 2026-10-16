from typing import Any

import pytest

from budget_bot.config import Settings
from budget_bot.models import Question
from tests.helpers import ALICE, BOB, PUBLIC_KEY


def test_default_is_dry_run() -> None:
    config = Settings.from_env()
    assert not config.live_messaging
    assert config.member_ids == (ALICE, BOB)


def test_second_member_can_be_added_later(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("DISCORD_MEMBER_2_ID")
    assert Settings.from_env().member_ids == (ALICE,)


def test_public_key_is_normalized(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("DISCORD_PUBLIC_KEY", f" {PUBLIC_KEY.upper()} ")
    assert Settings.from_env().public_key == PUBLIC_KEY


@pytest.mark.parametrize(
    ("name", "value"),
    [
        ("DISCORD_APPLICATION_ID", ""),
        ("DISCORD_APPLICATION_ID", "replace-with-application-id"),
        ("DISCORD_GUILD_ID", "1234"),
        ("DISCORD_CHANNEL_ID", "0" * 18),
        ("DISCORD_MEMBER_2_ID", ALICE),
        ("LIVE_MESSAGING_ENABLED", "yes"),
        ("DISCORD_PUBLIC_KEY", ""),
        ("DISCORD_PUBLIC_KEY", "replace-with-application-public-key"),
        ("DISCORD_PUBLIC_KEY", PUBLIC_KEY[:-2]),
        ("DISCORD_SECRET_NAME", " "),
    ],
)
def test_invalid_settings_fail_early(
    monkeypatch: pytest.MonkeyPatch, name: str, value: str
) -> None:
    monkeypatch.setenv(name, value)
    with pytest.raises(ValueError):
        Settings.from_env()


@pytest.mark.parametrize(
    "weak_key",
    [
        "00" * 32,
        "00" * 31 + "80",
        "01" + "00" * 31,
        "ec" + "ff" * 31,
        "26e8958fc2b227b045c3f489f2ef98f0d5dfac05d3c63339b13802886d53fc85",
    ],
)
def test_small_order_public_keys_are_rejected(
    monkeypatch: pytest.MonkeyPatch, weak_key: str
) -> None:
    monkeypatch.setenv("DISCORD_PUBLIC_KEY", weak_key)
    with pytest.raises(ValueError, match="DISCORD_PUBLIC_KEY"):
        Settings.from_env()


@pytest.mark.parametrize(
    "value",
    [
        None,
        [],
        {},
        {"interaction_id": 123, "user_id": ALICE, "token": "t"},
        {"interaction_id": "x", "user_id": ALICE, "token": "t"},
        {"interaction_id": "1", "user_id": ALICE, "text": [], "token": "t"},
        {"interaction_id": "1", "user_id": ALICE, "text": "q" * 501, "token": "t"},
        {"interaction_id": "1", "user_id": ALICE, "token": ""},
    ],
)
def test_invalid_queue_contract(value: Any) -> None:
    with pytest.raises(ValueError):
        Question.from_dict(value)


def test_ai_banking_and_alerts_are_off_by_default() -> None:
    config = Settings.from_env()
    assert not config.ai_enabled and not config.plaid_enabled
    assert (config.ai_daily_limit, config.ai_profile_scope) == (30, "us")
    assert config.plaid_secret_name == "household-budget/plaid"
    assert (config.alert_email, config.monthly_cost_alert_usd) == ("", 50)


def test_optional_features_can_be_enabled(monkeypatch: pytest.MonkeyPatch) -> None:
    for name, value in {
        "AI_ENABLED": "true",
        "AI_DAILY_LIMIT": "12",
        "AI_PROFILE_SCOPE": " Global ",
        "PLAID_ENABLED": "true",
        "PLAID_SECRET_NAME": "other/plaid",
        "ALERT_EMAIL": " me@example.com ",
        "MONTHLY_COST_ALERT_USD": "25",
    }.items():
        monkeypatch.setenv(name, value)
    config = Settings.from_env()
    assert config.ai_enabled and config.plaid_enabled
    assert (config.ai_daily_limit, config.ai_profile_scope) == (12, "global")
    assert (config.plaid_secret_name, config.alert_email, config.monthly_cost_alert_usd) == (
        "other/plaid",
        "me@example.com",
        25,
    )


@pytest.mark.parametrize(
    ("name", "value"),
    [
        ("AI_ENABLED", "yes"),
        ("PLAID_ENABLED", "1"),
        ("AI_DAILY_LIMIT", "0"),
        ("AI_DAILY_LIMIT", "501"),
        ("AI_DAILY_LIMIT", "many"),
        ("AI_PROFILE_SCOPE", "eu"),
        ("PLAID_SECRET_NAME", " "),
        ("ALERT_EMAIL", "not-an-email"),
        ("MONTHLY_COST_ALERT_USD", "0"),
        ("MONTHLY_COST_ALERT_USD", "free"),
    ],
)
def test_invalid_optional_settings_fail_early(
    monkeypatch: pytest.MonkeyPatch, name: str, value: str
) -> None:
    monkeypatch.setenv(name, value)
    with pytest.raises(ValueError):
        Settings.from_env()
