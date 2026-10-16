"""Synthetic card data for tests and the local demo. None of it is real spending."""

import calendar
import random
from dataclasses import dataclass
from datetime import date, timedelta
from decimal import Decimal

from budget_bot.ports import SyncBatch
from budget_bot.transactions import Transaction

CHASE = "chase-sapphire-preferred"
ROBINHOOD = "robinhood-gold-card"


@dataclass(frozen=True)
class _Template:
    merchant: str
    account: str
    primary: str
    detailed: str
    low: int  # cents
    high: int
    per_month: int


_TEMPLATES = (
    _Template("Whole Foods", CHASE, "FOOD_AND_DRINK", "FOOD_AND_DRINK_GROCERIES", 4500, 14000, 6),
    _Template(
        "Costco Wholesale",
        CHASE,
        "GENERAL_MERCHANDISE",
        "GENERAL_MERCHANDISE_SUPERSTORES",
        9000,
        32000,
        2,
    ),
    _Template(
        "Target",
        ROBINHOOD,
        "GENERAL_MERCHANDISE",
        "GENERAL_MERCHANDISE_SUPERSTORES",
        2500,
        12000,
        3,
    ),
    _Template(
        "Amazon",
        ROBINHOOD,
        "GENERAL_MERCHANDISE",
        "GENERAL_MERCHANDISE_ONLINE_MARKETPLACES",
        1500,
        9000,
        8,
    ),
    _Template(
        "Nordstrom",
        CHASE,
        "GENERAL_MERCHANDISE",
        "GENERAL_MERCHANDISE_CLOTHING_AND_ACCESSORIES",
        5000,
        18000,
        2,
    ),
    _Template("Starbucks", ROBINHOOD, "FOOD_AND_DRINK", "FOOD_AND_DRINK_COFFEE", 450, 1100, 10),
    _Template("Chipotle", CHASE, "FOOD_AND_DRINK", "FOOD_AND_DRINK_FAST_FOOD", 1100, 2600, 5),
    _Template("Local Bistro", CHASE, "FOOD_AND_DRINK", "FOOD_AND_DRINK_RESTAURANT", 4000, 11000, 4),
    _Template("Shell", ROBINHOOD, "TRANSPORTATION", "TRANSPORTATION_GAS", 3000, 6500, 3),
    _Template("Netflix", CHASE, "ENTERTAINMENT", "ENTERTAINMENT_TV_AND_MOVIES", 1599, 1599, 1),
    _Template(
        "Petco", ROBINHOOD, "GENERAL_MERCHANDISE", "GENERAL_MERCHANDISE_PET_SUPPLIES", 2000, 9000, 2
    ),
    _Template(
        "Planet Fitness",
        CHASE,
        "PERSONAL_CARE",
        "PERSONAL_CARE_GYMS_AND_FITNESS_CENTERS",
        2499,
        2499,
        1,
    ),
    _Template("Delta Air Lines", CHASE, "TRAVEL", "TRAVEL_FLIGHTS", 15000, 42000, 1),
    _Template(
        "State Farm", CHASE, "GENERAL_SERVICES", "GENERAL_SERVICES_INSURANCE", 12000, 12000, 1
    ),
)


def _money(cents: int) -> Decimal:
    return Decimal(cents) / 100


def _build(
    seed: int,
    yesterday: date,
    at: int,
    day: date,
    template: _Template,
    amount: Decimal,
) -> Transaction:
    posted = day + timedelta(days=1)
    pending = posted > yesterday
    return Transaction(
        txn_id=f"fixture:{seed}-{day.year}{day.month:02d}-{at:03d}",
        feed="fixture",
        account_key=template.account,
        amount=amount,
        spend_date=day,
        posted_date=None if pending else posted,
        pending=pending,
        merchant=template.merchant,
        provider_primary=template.primary,
        provider_detailed=template.detailed,
    )


_REFUND = _Template(
    "Amazon", ROBINHOOD, "GENERAL_MERCHANDISE", "GENERAL_MERCHANDISE_ONLINE_MARKETPLACES", 0, 0, 0
)
_PAYMENTS = tuple(
    _Template(
        "Payment Thank You", account, "LOAN_PAYMENTS", "LOAN_PAYMENTS_CREDIT_CARD_PAYMENT", 0, 0, 0
    )
    for account in (CHASE, ROBINHOOD)
)


def fixture_transactions(today: date, months_back: int = 2, seed: int = 1) -> list[Transaction]:
    """Purchases for the current and earlier months up to yesterday, plus payments and a refund."""
    yesterday = today - timedelta(days=1)
    found: list[Transaction] = []
    year, month = today.year, today.month
    for _ in range(months_back):
        year, month = (year - 1, 12) if month == 1 else (year, month - 1)
    while (year, month) <= (today.year, today.month):
        rng = random.Random(f"{seed}-{year}-{month}")
        days = calendar.monthrange(year, month)[1]
        counter = 0
        for template in _TEMPLATES:
            for _ in range(template.per_month):
                day = date(year, month, 1 + rng.randrange(days))
                if day <= yesterday:
                    amount = _money(rng.randrange(template.low, template.high + 1))
                    found.append(_build(seed, yesterday, counter, day, template, amount))
                counter += 1
        refund_day = date(year, month, min(days, 12))
        if refund_day <= yesterday:
            found.append(_build(seed, yesterday, 900, refund_day, _REFUND, -_money(2599)))
        payment_day = date(year, month, min(days, 20))
        if payment_day <= yesterday:
            for offset, payment in enumerate(_PAYMENTS):
                found.append(
                    _build(seed, yesterday, 950 + offset, payment_day, payment, -_money(250000))
                )
        year, month = (year + 1, 1) if month == 12 else (year, month + 1)
    return found


class FixtureProvider:
    """A TransactionProvider over synthetic data; lets the sync path run with no bank."""

    def __init__(self, today: date) -> None:
        self.today = today

    def feeds(self) -> list[str]:
        return ["fixture"]

    def fetch(self, feed: str, cursor: str | None) -> SyncBatch:
        return SyncBatch(
            upserts=tuple(fixture_transactions(self.today)),
            removed=(),
            cursor="fixture",
            accounts=(CHASE, ROBINHOOD),
        )
