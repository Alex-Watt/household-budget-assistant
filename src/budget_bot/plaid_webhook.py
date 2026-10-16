"""Plaid webhook receiver: verify the signed request, then ask the sync Lambda to run.

Plaid signs each webhook with a JWT in the `Plaid-Verification` header (ES256). The JWT's
claims carry the request body's SHA-256 and an issue time. Verification follows Plaid's
documented steps: require ES256, fetch the key by `kid`, check the signature, reject anything
older than five minutes, and compare the body hash in constant time.
"""

import base64
import hashlib
import hmac
import json
import logging
import os
import time
from collections.abc import Callable
from functools import lru_cache
from typing import Any

import boto3
from cryptography.exceptions import InvalidSignature
from cryptography.hazmat.primitives import hashes
from cryptography.hazmat.primitives.asymmetric import ec
from cryptography.hazmat.primitives.asymmetric.utils import encode_dss_signature

from budget_bot.config import Settings
from budget_bot.credentials import PlaidConfig, plaid_config
from budget_bot.plaid import PlaidClient

logger = logging.getLogger(__name__)
logger.setLevel(logging.INFO)

MAX_AGE_SECONDS = 300
MAX_BODY = 64_000
# New transactions, removals and item problems all mean "sync now"; the sync reads the rest.
SYNC_CODES = {
    "TRANSACTIONS": {
        "SYNC_UPDATES_AVAILABLE",
        "INITIAL_UPDATE",
        "HISTORICAL_UPDATE",
        "DEFAULT_UPDATE",
        "TRANSACTIONS_REMOVED",
    },
    "ITEM": {"ERROR", "LOGIN_REPAIRED", "PENDING_EXPIRATION", "PENDING_DISCONNECT"},
}
_keys: dict[str, dict[str, Any]] = {}
# Key IDs that failed lookup recently, so forged tokens cannot make us spend Plaid API calls.
_failed: dict[str, float] = {}
FAILURE_MEMORY_SECONDS = 60


# At most this many Plaid key lookups per minute per warm runtime, however many forged key IDs
# arrive. A real key is fetched once and then cached, so the limit never affects genuine traffic.
MAX_LOOKUPS_PER_MINUTE = 5
_lookups: list[float] = []


def _lookup_key(kid: str, fetch_key: Callable[[str], dict[str, Any]], now: float) -> dict[str, Any]:
    cached = _keys.get(kid)
    if cached is not None:
        return cached
    if _failed.get(kid, 0) > now:
        raise RejectedWebhook("key lookup failed recently")
    _lookups[:] = [moment for moment in _lookups if now - moment < 60]
    if len(_lookups) >= MAX_LOOKUPS_PER_MINUTE:
        raise RejectedWebhook("too many key lookups")
    _lookups.append(now)
    try:
        key = fetch_key(kid)
    except Exception as exc:
        # Remember any failure, including a malformed answer from Plaid, not just API errors.
        if len(_failed) >= 64:
            _failed.clear()
        _failed[kid] = now + FAILURE_MEMORY_SECONDS
        raise RejectedWebhook("key lookup failed") from exc
    if len(_keys) >= 16:
        _keys.clear()
    _keys[kid] = key
    return key


class RejectedWebhook(ValueError):
    pass


def _b64url(value: str) -> bytes:
    return base64.urlsafe_b64decode(value + "=" * (-len(value) % 4))


def _coordinate(value: Any) -> int:
    if not isinstance(value, str):
        raise RejectedWebhook("bad key")
    return int.from_bytes(_b64url(value), "big")


def verify_webhook(
    raw: bytes, token: str, fetch_key: Callable[[str], dict[str, Any]], now: float | None = None
) -> None:
    """Raise RejectedWebhook unless `token` is a fresh Plaid signature over exactly `raw`."""
    try:
        header_part, claims_part, signature_part = token.split(".")
        header = json.loads(_b64url(header_part))
        if header.get("alg") != "ES256" or not isinstance(header.get("kid"), str):
            raise RejectedWebhook("unsupported algorithm")
        key = _lookup_key(header["kid"], fetch_key, time.time() if now is None else now)
        if key.get("kty") != "EC" or key.get("crv") != "P-256" or key.get("alg") != "ES256":
            raise RejectedWebhook("unexpected key type")
        public_key = ec.EllipticCurvePublicNumbers(
            _coordinate(key.get("x")), _coordinate(key.get("y")), ec.SECP256R1()
        ).public_key()
        signature = _b64url(signature_part)
        if len(signature) != 64:
            raise RejectedWebhook("bad signature length")
        # JWT signatures are the raw r||s pair; the library wants DER.
        der = encode_dss_signature(
            int.from_bytes(signature[:32], "big"), int.from_bytes(signature[32:], "big")
        )
        public_key.verify(der, f"{header_part}.{claims_part}".encode(), ec.ECDSA(hashes.SHA256()))
        claims = json.loads(_b64url(claims_part))
        issued = claims["iat"]
        expired = key.get("expired_at")
        if (
            type(issued) is not int
            or abs((time.time() if now is None else now) - issued) > MAX_AGE_SECONDS
        ):
            raise RejectedWebhook("stale or invalid issue time")
        if expired is not None and issued > expired:
            raise RejectedWebhook("signed with an expired key")
        expected = claims["request_body_sha256"]
        if not isinstance(expected, str) or not hmac.compare_digest(
            hashlib.sha256(raw).hexdigest(), expected
        ):
            raise RejectedWebhook("body does not match signature")
    except RejectedWebhook:
        raise
    except (ValueError, KeyError, TypeError, InvalidSignature, AttributeError) as exc:
        raise RejectedWebhook("invalid webhook signature") from exc


def sync_target(payload: dict[str, Any], config: PlaidConfig) -> str | None:
    """The feed to sync for this webhook, or None if it needs no action."""
    kind, code, item_id = (payload.get(k) for k in ("webhook_type", "webhook_code", "item_id"))
    if not isinstance(kind, str) or code not in SYNC_CODES.get(kind, ()):
        return None
    if not isinstance(item_id, str) or config.item(item_id) is None:
        return None
    return f"plaid:{item_id}"


def handle(
    event: dict[str, Any],
    config: PlaidConfig,
    fetch_key: Callable[[str], dict[str, Any]],
    start_sync: Callable[[str], None],
    now: float | None = None,
) -> dict[str, Any]:
    if event.get("requestContext", {}).get("http", {}).get("method") != "POST":
        return {"statusCode": 405, "body": "Method Not Allowed"}
    try:
        body = event.get("body", "")
        if not isinstance(body, str):
            raise RejectedWebhook("bad body")
        raw = (
            base64.b64decode(body, validate=True) if event.get("isBase64Encoded") else body.encode()
        )
        if len(raw) > MAX_BODY:
            return {"statusCode": 413, "body": "Payload Too Large"}
        headers = {str(k).lower(): v for k, v in (event.get("headers") or {}).items()}
        verify_webhook(raw, headers.get("plaid-verification", ""), fetch_key, now)
        payload = json.loads(raw)
    except RejectedWebhook as exc:
        # Reasons are fixed strings from this module. Never log the body, token or headers.
        logger.warning("plaid_webhook rejected: %s", exc)
        return {"statusCode": 401, "body": "Unauthorized"}
    except ValueError, TypeError, UnicodeError:
        logger.warning("plaid_webhook rejected: malformed request")
        return {"statusCode": 401, "body": "Unauthorized"}
    feed = sync_target(payload, config) if isinstance(payload, dict) else None
    if feed is not None:
        start_sync(feed)
    # The signature is valid, so these two fields are Plaid's; still bound what is written.
    kind, code = (
        (payload.get("webhook_type"), payload.get("webhook_code"))
        if isinstance(payload, dict)
        else (None, None)
    )
    logger.info(
        "plaid_webhook verified type=%.40r code=%.40r action=%s",
        kind,
        code,
        "sync" if feed is not None else "none",
    )
    return {"statusCode": 200, "headers": {"Content-Type": "application/json"}, "body": "{}"}


@lru_cache(maxsize=1)
def _lambda_client() -> Any:
    return boto3.client("lambda")


def lambda_handler(event: dict[str, Any], context: Any) -> dict[str, Any]:
    settings = Settings.from_env()
    if not settings.plaid_enabled:
        # Nothing is linked, so nothing here can be real. Do not read secrets or verify.
        return {"statusCode": 404, "body": "Not Found"}
    config = plaid_config(settings.plaid_secret_name)
    # A short timeout: the Lambda allows 10 seconds, and a flood must not tie up its capacity.
    client = PlaidClient(config.environment, config.client_id, config.secret, timeout=4.0)

    def fetch_key(key_id: str) -> dict[str, Any]:
        key: dict[str, Any] = client.webhook_verification_key(key_id)["key"]
        return key

    def start_sync(feed: str) -> None:
        _lambda_client().invoke(
            FunctionName=os.environ["SYNC_FUNCTION"],
            InvocationType="Event",
            Payload=json.dumps({"feed": feed}).encode(),
        )

    return handle(event, config, fetch_key, start_sync)
