"""Budget categories, monthly caps, Plaid category mapping and merchant reporting rules."""

import re
from dataclasses import dataclass
from decimal import Decimal
from enum import StrEnum


class Category(StrEnum):
    SHOPPING = "shopping"
    GROCERIES = "groceries_household"
    DINING = "restaurants"
    HEALTH = "health_fitness_care"
    HOUSING = "housing"
    INSURANCE = "insurance_taxes_financial"
    ENTERTAINMENT = "entertainment"
    PETS = "pets"
    TRANSPORTATION = "transportation"
    TRAVEL = "travel"
    BILLS = "bills_utilities"
    SERVICES = "services"
    MISC = "miscellaneous"
    GIFTS = "gifts_donations"


@dataclass(frozen=True)
class CategoryInfo:
    category: Category
    label: str
    cap: Decimal
    aliases: tuple[str, ...] = ()


# Order and caps are the household's provisional card-spending targets (see the product brief).
CATEGORIES: tuple[CategoryInfo, ...] = (
    CategoryInfo(Category.SHOPPING, "Shopping", Decimal("1500"), ("shop", "clothes", "clothing")),
    CategoryInfo(
        Category.GROCERIES,
        "Groceries and household",
        Decimal("1300"),
        ("groceries", "grocery", "household", "groceries household"),
    ),
    CategoryInfo(
        Category.DINING,
        "Restaurants, takeout and coffee",
        Decimal("750"),
        ("restaurants", "restaurant", "dining", "dining out", "takeout", "coffee", "eating out"),
    ),
    CategoryInfo(
        Category.HEALTH,
        "Health, fitness and personal care",
        Decimal("675"),
        ("health", "fitness", "personal care", "medical", "gym"),
    ),
    CategoryInfo(
        Category.HOUSING,
        "Housing-related purchases",
        Decimal("360"),
        ("housing", "home", "home improvement"),
    ),
    CategoryInfo(
        Category.INSURANCE,
        "Insurance, taxes and financial",
        Decimal("285"),
        ("insurance", "taxes", "financial"),
    ),
    CategoryInfo(Category.ENTERTAINMENT, "Entertainment", Decimal("255"), ("fun",)),
    CategoryInfo(Category.PETS, "Pets", Decimal("255"), ("pet", "dog", "cat")),
    CategoryInfo(
        Category.TRANSPORTATION,
        "Transportation",
        Decimal("240"),
        ("transport", "gas", "car", "parking"),
    ),
    CategoryInfo(Category.TRAVEL, "Travel", Decimal("240"), ("vacation", "trip", "trips")),
    CategoryInfo(
        Category.BILLS, "Bills and utilities", Decimal("175"), ("bills", "utilities", "utility")
    ),
    CategoryInfo(Category.SERVICES, "Services", Decimal("95"), ("service",)),
    CategoryInfo(Category.MISC, "Miscellaneous", Decimal("95"), ("misc", "other")),
    CategoryInfo(
        Category.GIFTS,
        "Gifts and donations",
        Decimal("55"),
        ("gifts", "gift", "donations", "donation", "charity"),
    ),
)
INFO: dict[Category, CategoryInfo] = {info.category: info for info in CATEGORIES}
TOTAL_BUDGET = sum((info.cap for info in CATEGORIES), Decimal(0))
if TOTAL_BUDGET != Decimal("6280"):
    raise RuntimeError("category caps must total the $6,280 monthly card budget")


def label(category: Category) -> str:
    return INFO[category].label


def _normalize(text: str) -> str:
    return re.sub(
        r"\s+", " ", re.sub(r"[^a-z0-9 ]", " ", text.lower().replace("&", " and "))
    ).strip()


_PHRASES: list[tuple[str, Category]] = sorted(
    (
        (_normalize(phrase), info.category)
        for info in CATEGORIES
        for phrase in (info.category.value.replace("_", " "), info.label, *info.aliases)
    ),
    key=lambda pair: -len(pair[0]),
)


def mentions_category(text: str, category: Category) -> bool:
    """True if the text names this category (by id, label or any alias) as a whole phrase."""
    padded = f" {_normalize(text)} "
    return any(f" {phrase} " in padded for phrase, owner in _PHRASES if owner == category)


def find_category(text: str, exact: bool = False) -> Category | None:
    """Return the one category a short phrase refers to, or None if none or several match.

    With `exact`, the whole phrase must be a category name or alias.
    """
    normalized = _normalize(text)
    if not normalized:
        return None
    for phrase, category in _PHRASES:
        if phrase == normalized:
            return category
    if exact:
        return None
    padded = f" {normalized} "
    found = {category for phrase, category in _PHRASES if f" {phrase} " in padded}
    return next(iter(found)) if len(found) == 1 else None


# Plaid personal finance categories. Both the v1 and v2 taxonomies share these primary names;
# v2 added subcategories for income, loans, bank fees and transfers, none of which count as
# spending, so an unknown detailed value falls back to its primary category.
NON_SPEND_PRIMARY = frozenset(
    {"INCOME", "TRANSFER_IN", "TRANSFER_OUT", "LOAN_PAYMENTS", "LOAN_DISBURSEMENTS"}
)
# For these, only a credit (negative amount) is a payment to the card. A positive amount is a
# real charge, such as a loan payment made with the card, and counts as spending.
CREDIT_ONLY_PRIMARY = frozenset({"TRANSFER_OUT", "LOAN_PAYMENTS"})
_PRIMARY: dict[str, Category] = {
    "FOOD_AND_DRINK": Category.DINING,
    "GENERAL_MERCHANDISE": Category.SHOPPING,
    "HOME_IMPROVEMENT": Category.HOUSING,
    "MEDICAL": Category.HEALTH,
    "PERSONAL_CARE": Category.HEALTH,
    "GENERAL_SERVICES": Category.SERVICES,
    "GOVERNMENT_AND_NON_PROFIT": Category.MISC,
    "TRANSPORTATION": Category.TRANSPORTATION,
    "TRAVEL": Category.TRAVEL,
    "RENT_AND_UTILITIES": Category.BILLS,
    "ENTERTAINMENT": Category.ENTERTAINMENT,
    "BANK_FEES": Category.INSURANCE,
}
_DETAILED: dict[str, Category] = {
    "FOOD_AND_DRINK_GROCERIES": Category.GROCERIES,
    "FOOD_AND_DRINK_BEER_WINE_AND_LIQUOR": Category.GROCERIES,
    "GENERAL_MERCHANDISE_PET_SUPPLIES": Category.PETS,
    "MEDICAL_VETERINARY_SERVICES": Category.PETS,
    "GENERAL_MERCHANDISE_GIFTS_AND_NOVELTIES": Category.GIFTS,
    "GOVERNMENT_AND_NON_PROFIT_DONATIONS": Category.GIFTS,
    "GOVERNMENT_AND_NON_PROFIT_TAX_PAYMENT": Category.INSURANCE,
    "GENERAL_SERVICES_INSURANCE": Category.INSURANCE,
    "GENERAL_SERVICES_ACCOUNTING_AND_FINANCIAL_PLANNING": Category.INSURANCE,
    "GENERAL_SERVICES_AUTOMOTIVE": Category.TRANSPORTATION,
}


def map_provider_category(primary: str | None, detailed: str | None) -> Category:
    if detailed and detailed in _DETAILED:
        return _DETAILED[detailed]
    return _PRIMARY.get(primary or "", Category.MISC)


@dataclass(frozen=True)
class MerchantRule:
    rule_id: str
    pattern: re.Pattern[str]
    category: Category
    # The rule only moves purchases the provider put in one of these categories.
    only_from: frozenset[Category]


# Household reporting convention: Costco and Target count as groceries and household even when
# the aggregator says Shopping, or says nothing useful (a CSV with no category column).
# Costco gas, travel and the like keep their own provider category.
_UNDECIDED = frozenset({Category.SHOPPING, Category.MISC})
MERCHANT_RULES: tuple[MerchantRule, ...] = (
    MerchantRule(
        "costco", re.compile(r"\bcostco\b", re.IGNORECASE), Category.GROCERIES, _UNDECIDED
    ),
    MerchantRule(
        "target", re.compile(r"\btarget\b", re.IGNORECASE), Category.GROCERIES, _UNDECIDED
    ),
)


def apply_merchant_rules(merchant: str, current: Category) -> tuple[Category, str] | None:
    for rule in MERCHANT_RULES:
        if current in rule.only_from and rule.pattern.search(merchant):
            return rule.category, rule.rule_id
    return None
