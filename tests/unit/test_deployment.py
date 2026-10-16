import json
from typing import Any

import pytest

from scripts.register_commands import register
from scripts.validate_deploy import validate
from tests.helpers import ALICE, APP_ID, BOB, CHANNEL_ID, GUILD_ID, PUBLIC_KEY


def config() -> dict[str, str]:
    return {
        "AWS_ACCOUNT_ID": "123456789012",
        "AWS_REGION": "us-east-2",
        "AWS_DEPLOY_ROLE_ARN": "arn:aws:iam::123456789012:role/HouseholdBudgetGitHubDeployRole",
        "DISCORD_APPLICATION_ID": APP_ID,
        "DISCORD_PUBLIC_KEY": PUBLIC_KEY,
        "DISCORD_GUILD_ID": GUILD_ID,
        "DISCORD_CHANNEL_ID": CHANNEL_ID,
        "DISCORD_MEMBER_1_ID": ALICE,
        "DISCORD_MEMBER_2_ID": BOB,
    }


def test_single_account_config() -> None:
    validate(config())


@pytest.mark.parametrize(
    "changes",
    [
        {"AWS_ACCOUNT_ID": ""},
        {"AWS_REGION": ""},
        {"AWS_DEPLOY_ROLE_ARN": ""},
        {"AWS_DEPLOY_ROLE_ARN": "arn:aws:iam::999999999999:role/HouseholdBudgetGitHubDeployRole"},
        {"DISCORD_GUILD_ID": ""},
        {"DISCORD_PUBLIC_KEY": ""},
        {"LIVE_MESSAGING_ENABLED": "true"},
        {"PLAID_ENABLED": "true"},
        {"AI_ENABLED": "true"},
    ],
)
def test_invalid_deploy_config(changes: dict[str, Any]) -> None:
    with pytest.raises(ValueError):
        validate({**config(), **changes})


def test_register_overwrites_guild_commands_with_bot_token() -> None:
    requests = []

    class Response:
        def __enter__(self) -> Response:
            return self

        def __exit__(self, *_args: Any) -> None:
            pass

        def read(self, *_args: Any) -> bytes:
            return b'[{"name": "ask"}]'

    def opener(request: Any, timeout: float) -> Response:
        requests.append(request)
        return Response()

    assert register(APP_ID, GUILD_ID, "bot-token", opener) == ["ask"]
    request = requests[0]
    assert request.get_method() == "PUT"
    assert request.full_url == (
        f"https://discord.com/api/v10/applications/{APP_ID}/guilds/{GUILD_ID}/commands"
    )
    assert request.get_header("Authorization") == "Bot bot-token"
    option = json.loads(request.data)[0]["options"][0]
    assert (option["name"], option["required"], option["max_length"]) == ("question", True, 500)


@pytest.mark.parametrize(("app", "guild", "token"), [("", GUILD_ID, "t"), (APP_ID, GUILD_ID, "")])
def test_register_requires_ids_and_token(app: str, guild: str, token: str) -> None:
    with pytest.raises(ValueError):
        register(app, guild, token, lambda *_a, **_k: None)
