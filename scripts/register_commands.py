"""Register /ask in the household server. Run once after setup and after command changes.

Reads DISCORD_APPLICATION_ID and DISCORD_GUILD_ID from the environment. The bot token comes
from a hidden prompt and is never stored.
"""

import getpass
import json
import os
import urllib.request
from collections.abc import Callable
from typing import Any

from budget_bot.config import SNOWFLAKE
from budget_bot.discord import API, ASK_COMMAND, USER_AGENT
from budget_bot.models import MAX_QUESTION

COMMANDS = [
    {
        "name": ASK_COMMAND,
        "type": 1,
        "description": "Ask the household budget bot a question",
        "options": [
            {
                "type": 3,
                "name": "question",
                "description": "What do you want to know? Try: help",
                "required": True,
                "max_length": MAX_QUESTION,
            }
        ],
    }
]


def register(
    application_id: str, guild_id: str, bot_token: str, opener: Callable[..., Any]
) -> list[str]:
    if not SNOWFLAKE.fullmatch(application_id) or not SNOWFLAKE.fullmatch(guild_id):
        raise ValueError("Set DISCORD_APPLICATION_ID and DISCORD_GUILD_ID to Discord IDs")
    if not bot_token:
        raise ValueError("A bot token is required")
    request = urllib.request.Request(
        f"{API}/applications/{application_id}/guilds/{guild_id}/commands",
        data=json.dumps(COMMANDS).encode(),
        method="PUT",
        headers={
            "Authorization": f"Bot {bot_token}",
            "Content-Type": "application/json",
            "User-Agent": USER_AGENT,
        },
    )
    with opener(request, timeout=10) as response:
        return [command["name"] for command in json.load(response)]


if __name__ == "__main__":
    names = register(
        os.environ.get("DISCORD_APPLICATION_ID", ""),
        os.environ.get("DISCORD_GUILD_ID", ""),
        getpass.getpass("Discord bot token (hidden): ").strip(),
        urllib.request.urlopen,
    )
    print("Registered:", ", ".join(f"/{name}" for name in names))
