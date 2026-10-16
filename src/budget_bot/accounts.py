"""The household's two cards, by stable key. Keys never contain account numbers."""

import re

ACCOUNT_KEY = re.compile(r"[a-z0-9][a-z0-9-]{0,39}\Z")
KNOWN_ACCOUNTS: dict[str, str] = {
    "chase-sapphire-preferred": "Chase Sapphire Preferred",
    "robinhood-gold-card": "Robinhood Gold Card",
}


def account_label(key: str) -> str:
    return KNOWN_ACCOUNTS.get(key, key.replace("-", " ").title())


def validate_account_key(key: str) -> str:
    if not isinstance(key, str) or not ACCOUNT_KEY.fullmatch(key):
        raise ValueError("account key must be lowercase letters, digits and hyphens")
    return key
