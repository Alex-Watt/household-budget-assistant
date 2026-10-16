"""Read-only Plaid access: HTTP client, transaction normalization and incremental sync.

Only Plaid endpoints that read data are used. Plaid's Transactions product cannot move money,
and no payment or transfer product is requested. Amounts are parsed as Decimal, never float.
"""

import json
import re
import urllib.error
import urllib.request
from collections.abc import Callable, Mapping
from datetime import date
from decimal import Decimal
from typing import Any

from budget_bot.credentials import PlaidConfig, PlaidItem
from budget_bot.money import to_cents
from budget_bot.ports import ProviderError, SyncBatch
from budget_bot.transactions import Transaction, clean_text

BASES = {"sandbox": "https://sandbox.plaid.com", "production": "https://production.plaid.com"}
API_VERSION = "2020-09-14"
MAX_RESPONSE = 8_000_000
PAGE_SIZE = 500
MUTATION_RETRIES = 3
# Plaid reports many temporary problems as ordinary 4xx errors, so classify by code and type.
RETRYABLE_CODES = frozenset(
    {
        "INSTITUTION_DOWN",
        "INSTITUTION_NOT_RESPONDING",
        "INSTITUTION_NOT_AVAILABLE",
        "PLANNED_MAINTENANCE",
        "PRODUCT_NOT_READY",
        "INTERNAL_SERVER_ERROR",
        "RATE_LIMIT_EXCEEDED",
    }
)
RETRYABLE_TYPES = frozenset({"API_ERROR", "RATE_LIMIT_EXCEEDED"})
CATEGORY_NAME = re.compile(r"[A-Z][A-Z_]{0,79}\Z")


class PlaidError(ProviderError):
    """Plaid returned an error. `code` is Plaid's error_code, for example ITEM_LOGIN_REQUIRED."""

    def __init__(self, code: str, error_type: str = "", retryable: bool = True) -> None:
        super().__init__(code, retryable)
        self.error_type = error_type


class PlaidClient:
    def __init__(
        self,
        environment: str,
        client_id: str,
        secret: str,
        opener: Callable[..., Any] | None = None,
        timeout: float = 20.0,
    ) -> None:
        if environment not in BASES:
            raise ValueError("Plaid environment must be sandbox or production")
        self.base = BASES[environment]
        self._headers = {
            "Content-Type": "application/json",
            "PLAID-CLIENT-ID": client_id,
            "PLAID-SECRET": secret,
            "Plaid-Version": API_VERSION,
        }
        # Resolved now, not at import time, so tests can block the network.
        self.opener = opener or urllib.request.urlopen
        self.timeout = timeout

    def call(self, path: str, body: dict[str, Any]) -> dict[str, Any]:
        request = urllib.request.Request(
            f"{self.base}{path}",
            data=json.dumps(body).encode(),
            method="POST",
            headers=self._headers,
        )
        try:
            with self.opener(request, timeout=self.timeout) as response:
                payload = response.read(MAX_RESPONSE)
        except urllib.error.HTTPError as exc:
            # Plaid explains failures in a JSON body. Keep its error code, never the message.
            try:
                error = json.loads(exc.read(MAX_RESPONSE))
                code, kind = str(error["error_code"]), str(error.get("error_type", ""))
            except ValueError, KeyError, TypeError:
                code, kind = f"HTTP_{exc.code}", ""
            temporary = (
                exc.code >= 500
                or exc.code == 429
                or code in RETRYABLE_CODES
                or kind in RETRYABLE_TYPES
            )
            raise PlaidError(code, kind, retryable=temporary) from None
        except (urllib.error.URLError, TimeoutError) as exc:
            raise PlaidError("NETWORK_ERROR") from exc
        # Parse JSON numbers as Decimal so amounts are exact.
        result: dict[str, Any] = json.loads(payload, parse_float=Decimal)
        return result

    def link_token_create(
        self,
        user_id: str,
        webhook_url: str | None,
        days_requested: int = 180,
        access_token: str | None = None,
    ) -> dict[str, Any]:
        body: dict[str, Any] = {
            "client_name": "Household Budget",
            "language": "en",
            "country_codes": ["US"],
            "user": {"client_user_id": user_id},
            "hosted_link": {},
        }
        if access_token:
            # Update mode refreshes the same bank login. It does not create a second connection.
            body["access_token"] = access_token
        else:
            body["products"] = ["transactions"]
            body["transactions"] = {"days_requested": days_requested}
        if webhook_url:
            body["webhook"] = webhook_url
        return self.call("/link/token/create", body)

    def link_token_get(self, link_token: str) -> dict[str, Any]:
        return self.call("/link/token/get", {"link_token": link_token})

    def public_token_exchange(self, public_token: str) -> dict[str, Any]:
        return self.call("/item/public_token/exchange", {"public_token": public_token})

    def accounts_get(self, access_token: str) -> dict[str, Any]:
        return self.call("/accounts/get", {"access_token": access_token})

    def transactions_sync(self, access_token: str, cursor: str | None) -> dict[str, Any]:
        body: dict[str, Any] = {
            "access_token": access_token,
            "count": PAGE_SIZE,
            "options": {"personal_finance_category_version": "v2"},
        }
        if cursor:
            body["cursor"] = cursor
        return self.call("/transactions/sync", body)

    def webhook_verification_key(self, key_id: str) -> dict[str, Any]:
        return self.call("/webhook_verification_key/get", {"key_id": key_id})

    def item_remove(self, access_token: str) -> dict[str, Any]:
        return self.call("/item/remove", {"access_token": access_token})

    def sandbox_public_token(self, institution_id: str) -> dict[str, Any]:
        if self.base != BASES["sandbox"]:
            raise ValueError("sandbox tokens exist only in the sandbox environment")
        return self.call(
            "/sandbox/public_token/create",
            {"institution_id": institution_id, "initial_products": ["transactions"]},
        )


def _category(value: Any) -> str | None:
    return value if isinstance(value, str) and CATEGORY_NAME.fullmatch(value) else None


def normalize_transaction(
    raw: Mapping[str, Any], item_id: str, accounts: Mapping[str, str]
) -> Transaction | None:
    """Convert one Plaid transaction, or None for an account the household did not label."""
    account_key = accounts.get(raw["account_id"])
    if account_key is None:
        return None
    pending = bool(raw.get("pending"))
    posted = date.fromisoformat(raw["date"])
    authorized = raw.get("authorized_date")
    category = raw.get("personal_finance_category") or {}
    currency = raw.get("iso_currency_code") or "XXX"
    pending_of = raw.get("pending_transaction_id")
    return Transaction(
        txn_id=f"plaid:{raw['transaction_id']}",
        feed=f"plaid:{item_id}",
        account_key=account_key,
        amount=to_cents(Decimal(str(raw["amount"]))),
        spend_date=date.fromisoformat(authorized) if authorized else posted,
        posted_date=None if pending else posted,
        pending=pending,
        merchant=clean_text(str(raw.get("merchant_name") or raw.get("name") or ""))
        or "Unknown merchant",
        provider_primary=_category(category.get("primary")),
        provider_detailed=_category(category.get("detailed")),
        currency=currency if re.fullmatch(r"[A-Z]{3}", currency) else "XXX",
        pending_of=f"plaid:{pending_of}" if pending_of else None,
    )


class PlaidProvider:
    """Incremental /transactions/sync over every linked item."""

    def __init__(self, client: PlaidClient, config: PlaidConfig) -> None:
        self.client = client
        self.config = config

    def feeds(self) -> list[str]:
        return [f"plaid:{item.item_id}" for item in self.config.items]

    def _item(self, feed: str) -> PlaidItem:
        item = self.config.item(feed.removeprefix("plaid:")) if feed.startswith("plaid:") else None
        if item is None:
            raise ProviderError("UNKNOWN_FEED", retryable=False)
        return item

    def fetch(self, feed: str, cursor: str | None) -> SyncBatch:
        item = self._item(feed)
        for attempt in range(MUTATION_RETRIES + 1):
            try:
                return self._pull(item, cursor)
            except PlaidError as exc:
                # Plaid changed the data mid-pagination: restart from the first page's cursor.
                if exc.code != "TRANSACTIONS_SYNC_MUTATION_DURING_PAGINATION" or (
                    attempt == MUTATION_RETRIES
                ):
                    raise
        raise AssertionError("unreachable")

    def _pull(self, item: PlaidItem, cursor: str | None) -> SyncBatch:
        # Later pages and later entries win; a removal also cancels an earlier add.
        state: dict[str, Transaction | None] = {}
        position = cursor
        while True:
            page = self.client.transactions_sync(item.access_token, position)
            for raw in [*page["added"], *page["modified"]]:
                try:
                    txn = normalize_transaction(raw, item.item_id, item.accounts)
                except (KeyError, TypeError, ValueError, ArithmeticError) as exc:
                    # Fail loudly instead of skipping: a silent gap would understate spending.
                    raise ProviderError("BAD_TRANSACTION_DATA", retryable=False) from exc
                if txn is not None:
                    state[txn.txn_id] = txn
            for removed in page["removed"]:
                state[f"plaid:{removed['transaction_id']}"] = None
            position = page["next_cursor"] or position
            if not page["has_more"]:
                break
        return SyncBatch(
            upserts=tuple(txn for txn in state.values() if txn is not None),
            removed=tuple(txn_id for txn_id, txn in state.items() if txn is None),
            cursor=position or None,
            accounts=tuple(sorted(set(item.accounts.values()))),
        )
