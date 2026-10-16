import base64
import hashlib
import json
from collections.abc import Callable
from typing import Any

import pytest
from cryptography.hazmat.primitives import hashes
from cryptography.hazmat.primitives.asymmetric import ec
from cryptography.hazmat.primitives.asymmetric.utils import decode_dss_signature

from budget_bot import plaid_webhook
from budget_bot.credentials import PlaidConfig, PlaidItem
from budget_bot.plaid_webhook import RejectedWebhook, handle, sync_target, verify_webhook
from budget_bot.ports import ProviderError

NOW = 1_800_000_000
KEY = ec.generate_private_key(ec.SECP256R1())
OTHER_KEY = ec.generate_private_key(ec.SECP256R1())


def b64(data: bytes) -> str:
    return base64.urlsafe_b64encode(data).rstrip(b"=").decode()


def jwk(key: ec.EllipticCurvePrivateKey = KEY, **changes: Any) -> dict[str, Any]:
    numbers = key.public_key().public_numbers()
    base = {
        "alg": "ES256",
        "crv": "P-256",
        "kid": "key-1",
        "kty": "EC",
        "use": "sig",
        "expired_at": None,
        "x": b64(numbers.x.to_bytes(32, "big")),
        "y": b64(numbers.y.to_bytes(32, "big")),
    }
    return {**base, **changes}


def sign(
    body: bytes,
    key: ec.EllipticCurvePrivateKey = KEY,
    iat: int = NOW,
    alg: str = "ES256",
    claims: dict[str, Any] | None = None,
    kid: str = "key-1",
) -> str:
    header = b64(json.dumps({"alg": alg, "kid": kid, "typ": "JWT"}).encode())
    payload = claims or {"iat": iat, "request_body_sha256": hashlib.sha256(body).hexdigest()}
    claim_part = b64(json.dumps(payload).encode())
    r, s = decode_dss_signature(
        key.sign(f"{header}.{claim_part}".encode(), ec.ECDSA(hashes.SHA256()))
    )
    return f"{header}.{claim_part}.{b64(r.to_bytes(32, 'big') + s.to_bytes(32, 'big'))}"


BODY = json.dumps(
    {
        "webhook_type": "TRANSACTIONS",
        "webhook_code": "SYNC_UPDATES_AVAILABLE",
        "item_id": "item-1",
    },
    indent=2,
).encode()


class Keys:
    def __init__(self, key: dict[str, Any] | None = None, error: Exception | None = None) -> None:
        self.key, self.error, self.calls = key or jwk(), error, 0

    def __call__(self, key_id: str) -> dict[str, Any]:
        self.calls += 1
        assert key_id == "key-1"
        if self.error:
            raise self.error
        return self.key


@pytest.fixture(autouse=True)
def fresh_cache() -> None:
    plaid_webhook._keys.clear()
    plaid_webhook._failed.clear()
    plaid_webhook._lookups.clear()


def test_a_genuine_signature_over_the_exact_body_is_accepted() -> None:
    verify_webhook(BODY, sign(BODY), Keys(), NOW)


def test_the_body_hash_is_sensitive_to_whitespace() -> None:
    reformatted = json.dumps(json.loads(BODY)).encode()
    with pytest.raises(RejectedWebhook, match="body does not match"):
        verify_webhook(reformatted, sign(BODY), Keys(), NOW)


def test_a_changed_body_is_rejected() -> None:
    with pytest.raises(RejectedWebhook):
        verify_webhook(BODY.replace(b"item-1", b"item-2"), sign(BODY), Keys(), NOW)


def test_a_signature_from_another_key_is_rejected() -> None:
    with pytest.raises(RejectedWebhook):
        verify_webhook(BODY, sign(BODY, key=OTHER_KEY), Keys(), NOW)


@pytest.mark.parametrize("alg", ["HS256", "none", "RS256", "es256"])
def test_only_es256_is_accepted(alg: str) -> None:
    with pytest.raises(RejectedWebhook, match="algorithm"):
        verify_webhook(BODY, sign(BODY, alg=alg), Keys(), NOW)


@pytest.mark.parametrize(("offset", "ok"), [(0, True), (299, True), (301, False), (-301, False)])
def test_freshness_window_is_five_minutes_either_way(offset: int, ok: bool) -> None:
    token = sign(BODY, iat=NOW - offset)
    if ok:
        verify_webhook(BODY, token, Keys(), NOW)
    else:
        with pytest.raises(RejectedWebhook, match="issue time"):
            verify_webhook(BODY, token, Keys(), NOW)


def test_tampering_with_the_claims_breaks_the_signature() -> None:
    header, _, signature = sign(BODY).split(".")
    forged = b64(
        json.dumps(
            {"iat": NOW, "request_body_sha256": hashlib.sha256(b"other").hexdigest()}
        ).encode()
    )
    with pytest.raises(RejectedWebhook):
        verify_webhook(b"other", f"{header}.{forged}.{signature}", Keys(), NOW)


def test_a_key_that_expired_before_signing_is_rejected() -> None:
    with pytest.raises(RejectedWebhook, match="expired key"):
        verify_webhook(BODY, sign(BODY), Keys(jwk(expired_at=NOW - 10)), NOW)
    plaid_webhook._keys.clear()  # keys are cached by ID, so start the second case fresh
    verify_webhook(BODY, sign(BODY), Keys(jwk(expired_at=NOW + 10)), NOW)


@pytest.mark.parametrize("changes", [{"crv": "P-384"}, {"kty": "RSA"}, {"alg": "RS256"}, {"x": 5}])
def test_unexpected_keys_are_rejected(changes: dict[str, Any]) -> None:
    with pytest.raises(RejectedWebhook):
        verify_webhook(BODY, sign(BODY), Keys(jwk(**changes)), NOW)


@pytest.mark.parametrize("token", ["", "a.b", "a.b.c.d", "not a jwt", "....", "e30.e30.e30"])
def test_malformed_tokens_are_rejected(token: str) -> None:
    with pytest.raises(RejectedWebhook):
        verify_webhook(BODY, token, Keys(), NOW)


def test_claims_with_a_non_integer_issue_time_are_rejected() -> None:
    claims = {"iat": float(NOW), "request_body_sha256": hashlib.sha256(BODY).hexdigest()}
    with pytest.raises(RejectedWebhook):
        verify_webhook(BODY, sign(BODY, claims=claims), Keys(), NOW)


def test_the_key_is_fetched_once_and_cached() -> None:
    keys = Keys()
    verify_webhook(BODY, sign(BODY), keys, NOW)
    verify_webhook(BODY, sign(BODY), keys, NOW)
    assert keys.calls == 1


def test_failed_key_lookups_are_remembered_so_forgeries_cannot_burn_api_calls() -> None:
    keys = Keys(error=ProviderError("INVALID_FIELD"))
    for _ in range(5):
        with pytest.raises(RejectedWebhook):
            verify_webhook(BODY, sign(BODY), keys, NOW)
    assert keys.calls == 1
    with pytest.raises(RejectedWebhook):
        verify_webhook(BODY, sign(BODY), keys, NOW + 61)
    assert keys.calls == 2


@pytest.mark.parametrize("error", [KeyError("key"), ValueError("bad json"), TypeError("odd")])
def test_a_malformed_answer_from_plaid_is_remembered_too(error: Exception) -> None:
    keys = Keys(error=error)
    for _ in range(10):
        with pytest.raises(RejectedWebhook):
            verify_webhook(BODY, sign(BODY), keys, NOW)
    assert keys.calls == 1


def test_a_flood_of_forged_key_ids_makes_only_a_few_lookups() -> None:
    calls: list[str] = []

    def lookup(key_id: str) -> dict[str, Any]:
        calls.append(key_id)
        raise ProviderError("INVALID_FIELD")

    for number in range(1000):
        with pytest.raises(RejectedWebhook):
            verify_webhook(BODY, sign(BODY, kid=f"forged-{number}"), lookup, NOW)
    assert len(calls) == plaid_webhook.MAX_LOOKUPS_PER_MINUTE
    # A minute later the allowance refreshes, so a real key rotation still works.
    keys = Keys()
    verify_webhook(BODY, sign(BODY), keys, NOW + 61)
    assert keys.calls == 1


def test_the_lookup_limit_does_not_block_a_cached_key() -> None:
    keys = Keys()
    verify_webhook(BODY, sign(BODY), keys, NOW)

    def refuse(key_id: str) -> dict[str, Any]:
        raise ProviderError("INVALID_FIELD")

    for number in range(20):
        with pytest.raises(RejectedWebhook):
            verify_webhook(BODY, sign(BODY, kid=f"forged-{number}"), refuse, NOW)
    verify_webhook(BODY, sign(BODY), keys, NOW)  # the genuine key is still accepted


# The HTTP handler


def config() -> PlaidConfig:
    item = PlaidItem("item-1", "access-x", "Chase", {"acct": "chase-sapphire-preferred"})
    return PlaidConfig("client", "secret", "sandbox", (item,))


def event(
    body: bytes = BODY,
    token: str | None = None,
    method: str = "POST",
    encoded: bool = False,
    header: str = "plaid-verification",
) -> dict[str, Any]:
    payload = base64.b64encode(body).decode() if encoded else body.decode()
    return {
        "requestContext": {"http": {"method": method}},
        "headers": {header: token or sign(body)},
        "body": payload,
        "isBase64Encoded": encoded,
    }


def run(raw_event: dict[str, Any]) -> tuple[dict[str, Any], list[str]]:
    started: list[str] = []
    response = handle(raw_event, config(), Keys(), started.append, NOW)
    return response, started


def test_valid_webhook_for_a_linked_item_starts_a_sync() -> None:
    response, started = run(event())
    assert response["statusCode"] == 200 and started == ["plaid:item-1"]


def test_headers_are_case_insensitive_and_bodies_may_be_base64() -> None:
    _, started = run(event(header="Plaid-Verification", encoded=True))
    assert started == ["plaid:item-1"]


@pytest.mark.parametrize(
    "case",
    [
        lambda: event(token="forged.token.value"),
        lambda: {**event(), "headers": {}},
        lambda: {**event(), "body": BODY.decode().replace("item-1", "item-2")},
        lambda: {**event(), "body": 12345},
        lambda: {**event(), "isBase64Encoded": True},
    ],
)
def test_unverified_requests_get_401_and_start_nothing(case: Callable[[], dict[str, Any]]) -> None:
    response, started = run(case())
    assert response["statusCode"] == 401 and not started


def test_only_post_is_accepted() -> None:
    response, started = run(event(method="GET"))
    assert response["statusCode"] == 405 and not started


def test_oversized_bodies_are_refused_before_verification() -> None:
    response, started = run(event(body=b"x" * 70_000))
    assert response["statusCode"] == 413 and not started


def hook(kind: str, code: str, item: str | None = "item-1") -> dict[str, Any]:
    payload: dict[str, Any] = {"webhook_type": kind, "webhook_code": code}
    if item is not None:
        payload["item_id"] = item
    return payload


@pytest.mark.parametrize(
    ("payload", "target"),
    [
        (hook("TRANSACTIONS", "SYNC_UPDATES_AVAILABLE"), "plaid:item-1"),
        (hook("TRANSACTIONS", "TRANSACTIONS_REMOVED"), "plaid:item-1"),
        (hook("ITEM", "ERROR"), "plaid:item-1"),
        (hook("ITEM", "PENDING_EXPIRATION"), "plaid:item-1"),
        (hook("TRANSACTIONS", "SYNC_UPDATES_AVAILABLE", "other"), None),
        (hook("TRANSACTIONS", "RECURRING_TRANSACTIONS_UPDATE"), None),
        (hook("AUTH", "AUTOMATICALLY_VERIFIED"), None),
        (hook("TRANSACTIONS", "SYNC_UPDATES_AVAILABLE", None), None),
        ({"webhook_type": 5, "webhook_code": [], "item_id": {}}, None),
    ],
)
def test_which_webhooks_trigger_a_sync(payload: dict[str, Any], target: str | None) -> None:
    assert sync_target(payload, config()) == target


def test_a_verified_webhook_for_an_unknown_item_is_acknowledged_but_ignored() -> None:
    body = BODY.replace(b"item-1", b"gone")
    response, started = run(event(body=body))
    assert response["statusCode"] == 200 and not started


def test_a_verified_non_object_body_is_ignored() -> None:
    response, started = run(event(body=b"[1, 2]"))
    assert response["statusCode"] == 200 and not started


# What is written to the logs: outcomes and reasons, never the request itself.


@pytest.fixture
def webhook_logs(caplog: pytest.LogCaptureFixture) -> pytest.LogCaptureFixture:
    caplog.set_level("INFO", logger="budget_bot.plaid_webhook")
    return caplog


def test_a_verified_webhook_logs_its_type_code_and_action_only(
    webhook_logs: pytest.LogCaptureFixture,
) -> None:
    token = sign(BODY)
    run(event(token=token))
    run(event(body=BODY.replace(b"item-1", b"gone")))
    text = webhook_logs.text
    assert "verified type='TRANSACTIONS' code='SYNC_UPDATES_AVAILABLE' action=sync" in text
    assert "action=none" in text
    assert token not in text and "item-1" not in text and "gone" not in text
    assert "access-x" not in text and "secret" not in text.replace("plaid_webhook", "")


@pytest.mark.parametrize(
    ("case", "reason"),
    [
        (lambda: event(token="forged.token.value"), "rejected: invalid webhook signature"),
        (lambda: {**event(), "headers": {}}, "rejected: invalid webhook signature"),
        (lambda: {**event(), "body": 12345}, "rejected: bad body"),
    ],
)
def test_a_rejected_webhook_logs_the_reason_but_not_the_request(
    webhook_logs: pytest.LogCaptureFixture, case: Callable[[], dict[str, Any]], reason: str
) -> None:
    run(case())
    assert reason in webhook_logs.text
    assert "forged.token.value" not in webhook_logs.text and "item-1" not in webhook_logs.text


def test_a_signed_body_that_is_not_json_is_logged_as_malformed(
    webhook_logs: pytest.LogCaptureFixture,
) -> None:
    run(event(body=b"{not json"))
    assert "rejected: malformed request" in webhook_logs.text
    assert "not json" not in webhook_logs.text


def test_an_oversized_or_wrong_method_request_is_not_logged_in_detail(
    webhook_logs: pytest.LogCaptureFixture,
) -> None:
    run(event(body=b"x" * 70_000))
    run(event(method="GET"))
    assert "xxxx" not in webhook_logs.text
