from datetime import date
from decimal import Decimal

import pytest

from budget_bot.categories import Category
from budget_bot.transactions import (
    Override,
    Resolved,
    Transaction,
    clean_text,
    find_duplicates,
    resolve_all,
)
from tests.helpers import CHASE, ROBINHOOD, txn


def resolved_one(item: Transaction, overrides: dict[str, Override] | None = None) -> Resolved:
    return resolve_all([item], overrides or {})[0]


def test_clean_text_strips_control_and_format_characters() -> None:
    dirty = "AMAZON\n\tMKTP\u200b US\x00 " + "x" * 200
    cleaned = clean_text(dirty)
    assert cleaned.startswith("AMAZON MKTP US x")
    assert len(cleaned) == 80
    assert all(ch.isprintable() for ch in cleaned)


def test_transaction_validation() -> None:
    with pytest.raises(ValueError, match="whole cents"):
        txn(amount="1.005")
    with pytest.raises(ValueError, match="merchant"):
        txn(merchant="two\nlines")
    with pytest.raises(ValueError, match="merchant"):
        txn(merchant="")
    with pytest.raises(ValueError, match="invalid transaction"):
        txn(txn_id="bad id with spaces")
    with pytest.raises(ValueError, match="account key"):
        txn(account="Chase 1234")
    with pytest.raises(ValueError, match="ISO"):
        txn(currency="usd")


def test_item_round_trip_preserves_every_field() -> None:
    original = txn(
        "plaid:abc",
        "-5.25",
        primary="FOOD_AND_DRINK",
        detailed="FOOD_AND_DRINK_COFFEE",
        pending_of="plaid:old",
    )
    assert Transaction.from_item(original.to_item()) == original
    pending = txn("p", pending=True)
    assert Transaction.from_item(pending.to_item()) == pending


def test_provider_category_is_used_with_provenance() -> None:
    result = resolved_one(
        txn(primary="FOOD_AND_DRINK", detailed="FOOD_AND_DRINK_GROCERIES", merchant="Aldi")
    )
    assert result.category == Category.GROCERIES
    assert result.provenance == "provider:FOOD_AND_DRINK_GROCERIES"


def test_no_provider_category_falls_back_to_miscellaneous() -> None:
    result = resolved_one(txn(merchant="Mystery"))
    assert (result.category, result.provenance) == (Category.MISC, "default")


def test_costco_and_target_count_as_groceries_with_original_category_kept() -> None:
    target = txn(
        "t",
        merchant="Target",
        primary="GENERAL_MERCHANDISE",
        detailed="GENERAL_MERCHANDISE_SUPERSTORES",
    )
    result = resolved_one(target)
    assert result.category == Category.GROCERIES
    assert result.provenance == "merchant_rule:target"
    # The provider's own category is preserved on the record for audit and reversal.
    assert result.txn.provider_detailed == "GENERAL_MERCHANDISE_SUPERSTORES"


def test_user_override_beats_merchant_rule_and_is_reversible() -> None:
    target = txn(
        "t",
        merchant="Target",
        primary="GENERAL_MERCHANDISE",
        detailed="GENERAL_MERCHANDISE_SUPERSTORES",
    )
    override = Override("t", Category.GIFTS, "100000000000000004", "2026-10-14T13:00:00+00:00")
    assert resolved_one(target, {"t": override}).category == Category.GIFTS
    assert resolved_one(target, {"t": override}).provenance == "user"
    assert resolved_one(target, {}).category == Category.GROCERIES


@pytest.mark.parametrize(
    ("primary", "reason"),
    [
        ("LOAN_PAYMENTS", "payment_or_transfer"),
        ("TRANSFER_IN", "payment_or_transfer"),
        ("TRANSFER_OUT", "payment_or_transfer"),
        ("LOAN_DISBURSEMENTS", "payment_or_transfer"),
        ("INCOME", "income_or_credit"),
    ],
)
def test_payments_transfers_and_income_never_count_as_spending(primary: str, reason: str) -> None:
    result = resolved_one(txn(amount="-2500.00", primary=primary, merchant="Payment"))
    assert (result.counted, result.excluded) == (False, reason)


def test_uncategorized_card_payment_is_recognized_by_name() -> None:
    assert not resolved_one(txn(amount="-300.00", merchant="AUTOMATIC PAYMENT - THANK YOU")).counted
    # A refund with a real merchant name and no category still counts.
    assert resolved_one(txn(amount="-30.00", merchant="Nordstrom")).counted


def test_refund_is_negative_spending_in_its_category() -> None:
    result = resolved_one(txn(amount="-25.99", primary="GENERAL_MERCHANDISE", merchant="Amazon"))
    assert result.counted and result.category == Category.SHOPPING


def test_non_usd_is_excluded_even_with_an_override() -> None:
    foreign = txn("f", currency="EUR")
    override = Override("f", Category.TRAVEL, "u", "2026-10-14T13:00:00+00:00")
    assert resolved_one(foreign, {"f": override}).excluded == "non_usd_currency"


def test_user_can_include_a_wrongly_excluded_transaction() -> None:
    mislabeled = txn("m", amount="-40.00", primary="INCOME", merchant="Payroll deposit")
    override = Override("m", Category.SHOPPING, "u", "2026-10-14T13:00:00+00:00")
    assert not resolved_one(mislabeled).counted
    assert resolved_one(mislabeled, {"m": override}).counted


def test_a_store_return_counts_even_when_the_bank_calls_it_a_transfer_or_income() -> None:
    amazon = resolved_one(
        txn(
            "a",
            amount="-30.00",
            merchant="AMZN Mktp",
            primary="TRANSFER_IN",
            detailed="TRANSFER_IN_ACCOUNT_TRANSFER",
        )
    )
    nordstrom = resolved_one(txn("n", amount="-20.00", merchant="Nordstrom", primary="INCOME"))
    costco = resolved_one(txn("c", amount="-15.00", merchant="Costco", primary="TRANSFER_IN"))
    assert (amazon.counted, amazon.category, amazon.provenance) == (
        True,
        Category.SHOPPING,
        "refund:amazon",
    )
    assert (nordstrom.counted, nordstrom.category) == (True, Category.MISC)
    assert (costco.counted, costco.category) == (True, Category.GROCERIES)


@pytest.mark.parametrize(
    "merchant",
    ["Payment", "AUTOMATIC PAYMENT - THANK YOU", "ACME PAYROLL", "ULTIMATE REWARDS", "IRS TREAS"],
)
def test_payments_paychecks_and_rewards_are_not_refunds(merchant: str) -> None:
    credit = txn("c", amount="-25.00", merchant=merchant, primary="INCOME")
    assert not resolved_one(credit).counted


def test_pending_record_is_dropped_once_its_posted_replacement_arrives() -> None:
    pending = txn("pend", "12.00", pending=True, merchant="Cafe")
    posted = txn("post", "12.00", merchant="Cafe", pending_of="pend")
    assert find_duplicates([pending, posted]) == {"pend": "replaced_by:post"}
    counted = [r for r in resolve_all([pending, posted], {}) if r.counted]
    assert [r.txn.txn_id for r in counted] == ["post"]


def test_same_purchase_in_two_feeds_counts_once_preferring_plaid() -> None:
    plaid = txn("plaid:1", "40.00", feed="plaid:item1")
    csv = txn("csv:9", "40.00", feed="csv:chase-sapphire-preferred", merchant="AMZN MKTP")
    assert find_duplicates([csv, plaid]) == {"csv:9": "duplicate_of:plaid:1"}


def test_duplicate_plaid_items_for_one_card_count_once() -> None:
    first = txn("a1", "40.00", feed="plaid:item1")
    second = txn("b1", "40.00", feed="plaid:item2")
    assert find_duplicates([second, first]) == {"b1": "duplicate_of:a1"}


def test_two_identical_purchases_in_one_feed_both_count() -> None:
    twins = [txn("x1", "5.00"), txn("x2", "5.00")]
    assert find_duplicates(twins) == {}


def test_extra_records_beyond_the_overlap_still_count() -> None:
    plaid = [txn("p1", "5.00", feed="plaid:item1")]
    csv = [txn("c1", "5.00", feed="csv:x"), txn("c2", "5.00", feed="csv:x")]
    assert find_duplicates([*plaid, *csv]) == {"c1": "duplicate_of:p1"}


def test_different_cards_with_matching_amounts_are_not_duplicates() -> None:
    first = txn("a", "9.99", account=CHASE, feed="plaid:item1")
    second = txn("b", "9.99", account=ROBINHOOD, feed="plaid:item2")
    assert find_duplicates([first, second]) == {}


def test_amount_is_stored_as_decimal_cents() -> None:
    assert isinstance(txn(amount="19.99").amount, Decimal)
    assert resolved_one(txn(spend=date(2026, 10, 5))).txn.spend_date == date(2026, 10, 5)
