"""Manual CSV import for a card Plaid cannot read, or for backfilling history.

Rows get stable IDs from their own contents, so importing the same export twice, or two
exports that overlap, never double counts. Descriptions are untrusted text and are cleaned.
Nothing in a CSV is ever echoed into an error message.
"""

import csv
import hashlib
import io
from collections import Counter
from dataclasses import dataclass
from datetime import date, datetime

from budget_bot.accounts import validate_account_key
from budget_bot.money import parse_amount
from budget_bot.transactions import Transaction, clean_text

MAX_CHARS = 5_000_000
MAX_ROWS = 50_000
DATE_FORMATS = ("%m/%d/%Y", "%Y-%m-%d", "%m/%d/%y")


@dataclass(frozen=True)
class CsvFormat:
    date_col: str
    amount_col: str
    description_col: str
    post_date_col: str | None = None
    category_col: str | None = None
    type_col: str | None = None
    # Chase exports purchases as negative numbers; some issuers use positive.
    spend_is_negative: bool = True


CHASE = CsvFormat(
    "Transaction Date",
    "Amount",
    "Description",
    post_date_col="Post Date",
    category_col="Category",
    type_col="Type",
)
FORMATS = {"chase": CHASE, "generic": CsvFormat("Date", "Amount", "Description")}

# Chase's category column to Plaid-style categories, so the same budget mapping applies.
CHASE_CATEGORIES: dict[str, tuple[str, str | None]] = {
    "groceries": ("FOOD_AND_DRINK", "FOOD_AND_DRINK_GROCERIES"),
    "food & drink": ("FOOD_AND_DRINK", None),
    "shopping": ("GENERAL_MERCHANDISE", None),
    "gas": ("TRANSPORTATION", "TRANSPORTATION_GAS"),
    "automotive": ("GENERAL_SERVICES", "GENERAL_SERVICES_AUTOMOTIVE"),
    "travel": ("TRAVEL", None),
    "entertainment": ("ENTERTAINMENT", None),
    "health & wellness": ("PERSONAL_CARE", None),
    "personal": ("PERSONAL_CARE", None),
    "bills & utilities": ("RENT_AND_UTILITIES", None),
    "home": ("HOME_IMPROVEMENT", None),
    "professional services": ("GENERAL_SERVICES", None),
    "education": ("GENERAL_SERVICES", "GENERAL_SERVICES_EDUCATION"),
    "gifts & donations": ("GOVERNMENT_AND_NON_PROFIT", "GOVERNMENT_AND_NON_PROFIT_DONATIONS"),
    "fees & adjustments": ("BANK_FEES", None),
}


def _field(row: dict[str, str | None], column: str) -> str:
    """A cell's text. A short row leaves its later cells as None, which means a malformed row."""
    value = row[column]
    if value is None:
        raise ValueError("missing cell")
    return value


def _parse_date(text: str) -> date:
    for pattern in DATE_FORMATS:
        try:
            return datetime.strptime(text.strip(), pattern).date()
        except ValueError:
            continue
    raise ValueError("invalid date")


def parse_csv(text: str, account_key: str, fmt: CsvFormat = CHASE) -> list[Transaction]:
    """Parse one export into transactions. Raises ValueError naming the row, never its contents."""
    validate_account_key(account_key)
    if len(text) > MAX_CHARS:
        raise ValueError("file is too large")
    reader = csv.DictReader(io.StringIO(text))
    wanted = [c for c in (fmt.date_col, fmt.amount_col, fmt.description_col) if c]
    missing = [column for column in wanted if column not in (reader.fieldnames or [])]
    if missing:
        raise ValueError(f"missing columns: {', '.join(missing)}")
    seen: Counter[tuple[str, str, str]] = Counter()
    found: list[Transaction] = []
    for number, row in enumerate(reader, start=2):
        if number > MAX_ROWS:
            raise ValueError("too many rows")
        try:
            found.append(_row(row, account_key, fmt, seen))
        except (ValueError, KeyError, TypeError, AttributeError) as exc:
            raise ValueError(f"row {number} could not be read") from exc
    return found


def _row(
    row: dict[str, str | None],
    account_key: str,
    fmt: CsvFormat,
    seen: Counter[tuple[str, str, str]],
) -> Transaction:
    spent_on = _parse_date(_field(row, fmt.date_col))
    post_text = (row.get(fmt.post_date_col) or "").strip() if fmt.post_date_col else ""
    posted = _parse_date(post_text) if post_text else spent_on
    amount = parse_amount(_field(row, fmt.amount_col))
    amount = -amount if fmt.spend_is_negative else amount
    description = clean_text(_field(row, fmt.description_col)) or "Unknown merchant"
    kind = (row.get(fmt.type_col) or "").strip().lower() if fmt.type_col else ""
    category = (row.get(fmt.category_col) or "").strip().lower() if fmt.category_col else ""
    primary, detailed = CHASE_CATEGORIES.get(category, (None, None))
    if kind == "payment":
        primary, detailed = "LOAN_PAYMENTS", "LOAN_PAYMENTS_CREDIT_CARD_PAYMENT"
    # Two identical rows on one day are two purchases; the ordinal keeps their IDs distinct.
    key = (posted.isoformat(), str(amount), description.lower())
    ordinal = seen[key]
    seen[key] += 1
    digest = hashlib.sha256("|".join((account_key, *key, str(ordinal))).encode()).hexdigest()[:24]
    return Transaction(
        txn_id=f"csv:{digest}",
        feed=f"csv:{account_key}",
        account_key=account_key,
        amount=amount,
        spend_date=spent_on,
        posted_date=posted,
        pending=False,
        merchant=description,
        provider_primary=primary,
        provider_detailed=detailed,
    )
