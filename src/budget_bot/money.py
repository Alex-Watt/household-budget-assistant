"""Decimal-safe money helpers. A float never represents a dollar amount in this codebase."""

import re
from decimal import ROUND_HALF_UP, Decimal, InvalidOperation

CENT = Decimal("0.01")
MAX_AMOUNT = Decimal("10000000")


def cents(value: Decimal) -> Decimal:
    return value.quantize(CENT, rounding=ROUND_HALF_UP)


AMOUNT = re.compile(r"[+-]?(?:\d{1,3}(?:,\d{3})+|\d+)(?:\.\d+)?\Z")


def parse_amount(text: str) -> Decimal:
    """Parse '$1,234.50', '(12.00)' (accounting negative) or '-3' into cents.

    Commas must be thousands separators: '12,50' is rejected rather than read as 1,250.
    """
    cleaned = text.strip().replace("$", "").replace(" ", "")
    negative = cleaned.startswith("(") and cleaned.endswith(")")
    if negative:
        cleaned = cleaned[1:-1]
    if not AMOUNT.fullmatch(cleaned):
        raise ValueError("invalid amount")
    try:
        value = Decimal(cleaned.replace(",", ""))
    except InvalidOperation as exc:
        raise ValueError("invalid amount") from exc
    return to_cents(-value if negative else value)


def to_cents(value: Decimal | int | str) -> Decimal:
    """Convert to cents, rejecting NaN, infinity and absurd magnitudes."""
    try:
        amount = value if isinstance(value, Decimal) else Decimal(value)
    except InvalidOperation as exc:
        raise ValueError("invalid amount") from exc
    if not amount.is_finite() or abs(amount) > MAX_AMOUNT:
        raise ValueError("amount out of range")
    return cents(amount)


def usd(value: Decimal) -> str:
    amount = cents(value)
    sign = "-" if amount < 0 else ""
    return f"{sign}${abs(amount):,.2f}"


def percent(part: Decimal, whole: Decimal) -> int | None:
    """Whole-number percentage, or None when there is nothing to divide by."""
    if whole <= 0:
        return None
    return int((part * 100 / whole).quantize(Decimal(1), rounding=ROUND_HALF_UP))
