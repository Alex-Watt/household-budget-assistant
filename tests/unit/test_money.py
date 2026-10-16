from decimal import Decimal

import pytest

from budget_bot.money import cents, parse_amount, percent, to_cents, usd


def test_usd_formats_cents_with_separators_and_sign() -> None:
    assert usd(Decimal("1234.5")) == "$1,234.50"
    assert usd(Decimal("-12.345")) == "-$12.35"
    assert usd(Decimal("-0.001")) == "$0.00"


def test_rounding_is_half_up_not_bankers() -> None:
    assert cents(Decimal("0.125")) == Decimal("0.13")
    assert cents(Decimal("2.675")) == Decimal("2.68")


@pytest.mark.parametrize(
    ("text", "expected"),
    [("$1,234.50", "1234.50"), ("(12.00)", "-12.00"), ("-3", "-3.00"), (" 7 ", "7.00")],
)
def test_parse_amount(text: str, expected: str) -> None:
    assert parse_amount(text) == Decimal(expected)


@pytest.mark.parametrize("text", ["", "abc", "NaN", "Infinity", "1e99", "12.3.4"])
def test_parse_amount_rejects_garbage(text: str) -> None:
    with pytest.raises(ValueError):
        parse_amount(text)


def test_to_cents_rejects_absurd_values() -> None:
    with pytest.raises(ValueError):
        to_cents(Decimal("1e12"))


def test_percent_rounds_half_up_and_guards_zero() -> None:
    assert percent(Decimal("1"), Decimal("8")) == 13
    assert percent(Decimal("5"), Decimal("0")) is None
