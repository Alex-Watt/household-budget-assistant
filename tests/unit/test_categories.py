from decimal import Decimal

import pytest

from budget_bot.categories import (
    CATEGORIES,
    TOTAL_BUDGET,
    Category,
    apply_merchant_rules,
    find_category,
    map_provider_category,
)


def test_caps_total_the_household_budget() -> None:
    assert TOTAL_BUDGET == Decimal("6280")
    assert len(CATEGORIES) == len(Category) == 14
    assert {info.category for info in CATEGORIES} == set(Category)


def test_caps_match_the_brief_exactly() -> None:
    caps = {info.category: info.cap for info in CATEGORIES}
    assert caps[Category.SHOPPING] == 1500
    assert caps[Category.GROCERIES] == 1300
    assert caps[Category.DINING] == 750
    assert caps[Category.GIFTS] == 55


@pytest.mark.parametrize(
    ("primary", "detailed", "expected"),
    [
        ("FOOD_AND_DRINK", "FOOD_AND_DRINK_GROCERIES", Category.GROCERIES),
        ("FOOD_AND_DRINK", "FOOD_AND_DRINK_COFFEE", Category.DINING),
        ("FOOD_AND_DRINK", None, Category.DINING),
        ("GENERAL_MERCHANDISE", "GENERAL_MERCHANDISE_PET_SUPPLIES", Category.PETS),
        ("GENERAL_MERCHANDISE", "GENERAL_MERCHANDISE_GIFTS_AND_NOVELTIES", Category.GIFTS),
        ("GENERAL_MERCHANDISE", "GENERAL_MERCHANDISE_SUPERSTORES", Category.SHOPPING),
        ("MEDICAL", "MEDICAL_VETERINARY_SERVICES", Category.PETS),
        ("MEDICAL", "MEDICAL_DENTAL_CARE", Category.HEALTH),
        ("PERSONAL_CARE", "PERSONAL_CARE_GYMS_AND_FITNESS_CENTERS", Category.HEALTH),
        ("GENERAL_SERVICES", "GENERAL_SERVICES_INSURANCE", Category.INSURANCE),
        ("GENERAL_SERVICES", "GENERAL_SERVICES_AUTOMOTIVE", Category.TRANSPORTATION),
        ("GOVERNMENT_AND_NON_PROFIT", "GOVERNMENT_AND_NON_PROFIT_DONATIONS", Category.GIFTS),
        ("GOVERNMENT_AND_NON_PROFIT", "GOVERNMENT_AND_NON_PROFIT_TAX_PAYMENT", Category.INSURANCE),
        ("RENT_AND_UTILITIES", "RENT_AND_UTILITIES_WATER", Category.BILLS),
        ("HOME_IMPROVEMENT", "HOME_IMPROVEMENT_FURNITURE", Category.HOUSING),
        ("BANK_FEES", "BANK_FEES_INTEREST_CHARGE", Category.INSURANCE),
        # A future subcategory still lands in its primary's category; unknown primaries go misc.
        ("FOOD_AND_DRINK", "FOOD_AND_DRINK_SOMETHING_NEW", Category.DINING),
        ("BRAND_NEW_PRIMARY", None, Category.MISC),
        (None, None, Category.MISC),
    ],
)
def test_provider_category_mapping(
    primary: str | None, detailed: str | None, expected: Category
) -> None:
    assert map_provider_category(primary, detailed) == expected


@pytest.mark.parametrize(
    "merchant", ["COSTCO WHSE #123", "Costco.com", "Target", "TARGET.COM 8003"]
)
def test_costco_and_target_move_shopping_to_groceries(merchant: str) -> None:
    assert apply_merchant_rules(merchant, Category.SHOPPING) is not None
    assert apply_merchant_rules(merchant, Category.SHOPPING)[0] == Category.GROCERIES  # type: ignore[index]


@pytest.mark.parametrize(
    ("merchant", "provider"),
    [
        ("Costco Gas", Category.TRANSPORTATION),
        ("Costco Travel", Category.TRAVEL),
        ("Target", Category.PETS),
        ("Targeted Ads Inc", Category.SHOPPING),
        ("Whole Foods", Category.SHOPPING),
    ],
)
def test_rule_leaves_other_categories_and_merchants_alone(
    merchant: str, provider: Category
) -> None:
    assert apply_merchant_rules(merchant, provider) is None


@pytest.mark.parametrize(
    ("text", "expected"),
    [
        ("shopping", Category.SHOPPING),
        ("Dining", Category.DINING),
        ("dining out", Category.DINING),
        ("groceries & household", Category.GROCERIES),
        ("Restaurants, takeout and coffee", Category.DINING),
        ("how is the pet budget", Category.PETS),
        ("gas", Category.TRANSPORTATION),
        ("health", Category.HEALTH),
        ("gifts and donations", Category.GIFTS),
    ],
)
def test_find_category(text: str, expected: Category) -> None:
    assert find_category(text) == expected


@pytest.mark.parametrize("text", ["", "hello", "dining and shopping"])
def test_find_category_returns_none_when_missing_or_ambiguous(text: str) -> None:
    assert find_category(text) is None
