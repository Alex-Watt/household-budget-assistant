"""Import a card's CSV export into the ledger. Shows a summary and changes nothing without --apply.

Example (PowerShell, after `aws login`):
    python scripts/import_csv.py chase.csv --account chase-sapphire-preferred --format chase
    python scripts/import_csv.py chase.csv --account chase-sapphire-preferred --apply

Re-importing the same file, or an overlapping export, is safe: rows have stable IDs.
Nothing is printed from inside the file except totals.
"""

import argparse
import os
from dataclasses import dataclass, replace
from datetime import UTC, date, datetime, time
from decimal import Decimal
from pathlib import Path

from budget_bot.accounts import KNOWN_ACCOUNTS
from budget_bot.aws_login import sign_in_or_exit
from budget_bot.budget import HOUSEHOLD_TIME
from budget_bot.csv_import import FORMATS, CsvFormat, parse_csv
from budget_bot.ledger import DynamoLedger, ledger_table
from budget_bot.money import usd
from budget_bot.ports import FeedStatus
from budget_bot.transactions import resolve_all


@dataclass(frozen=True)
class ImportSummary:
    rows: int
    first: date | None
    last: date | None
    spending: Decimal
    ignored: int  # payments and other non-spending rows
    applied: bool


def import_csv(
    text: str,
    account: str,
    fmt: CsvFormat,
    ledger: DynamoLedger | None,
    now: datetime,
) -> ImportSummary:
    transactions = parse_csv(text, account, fmt)
    resolved = resolve_all(transactions, {})
    counted = [item for item in resolved if item.counted]
    spending = sum((item.txn.amount for item in counted), Decimal(0))
    dates = [txn.spend_date for txn in transactions]
    if ledger is not None:
        if spending < 0:
            # Real spending is positive. A negative total almost always means the amount signs
            # are reversed for this issuer, which would silently turn purchases into credits.
            raise ValueError("Net spending is negative; the sign of the amounts may be reversed.")
        ledger.upsert(transactions)
        # The card is "current" only as far as the file goes, not as of the day it was imported,
        # so a file that ends in August is reported as August data and goes stale honestly.
        newest = max((txn.posted_date or txn.spend_date for txn in transactions), default=None)
        through = (
            datetime.combine(newest, time(23, 59, 59), tzinfo=HOUSEHOLD_TIME) if newest else now
        )
        ledger.save_status(
            FeedStatus(f"csv:{account}", through.astimezone(UTC).isoformat(), None, (account,))
        )
    return ImportSummary(
        rows=len(transactions),
        first=min(dates, default=None),
        last=max(dates, default=None),
        spending=spending,
        ignored=len(resolved) - len(counted),
        applied=ledger is not None,
    )


def main() -> None:
    parser = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawTextHelpFormatter
    )
    parser.add_argument("file", type=Path)
    parser.add_argument(
        "--account",
        required=True,
        choices=sorted(KNOWN_ACCOUNTS),
        help="which household card this file is for",
    )
    parser.add_argument("--format", choices=sorted(FORMATS), default="chase")
    parser.add_argument(
        "--spending",
        choices=["negative", "positive"],
        help="generic format only: are purchases negative or positive numbers in this file?",
    )
    parser.add_argument("--table", default=os.environ.get("LEDGER_TABLE", ""))
    parser.add_argument("--apply", action="store_true", help="write to the ledger")
    args = parser.parse_args()
    fmt = FORMATS[args.format]
    if args.format == "generic":
        if args.spending is None:
            raise SystemExit("The generic format needs --spending negative or --spending positive.")
        fmt = replace(fmt, spend_is_negative=args.spending == "negative")
    ledger = None
    if args.apply:
        if not args.table:
            raise SystemExit("Pass --table with the Ledger table name (a stack output).")
        sign_in_or_exit()
        ledger = DynamoLedger(ledger_table(args.table))
    summary = import_csv(
        args.file.read_text(encoding="utf-8-sig"),
        args.account,
        fmt,
        ledger,
        datetime.now(UTC),
    )
    print(
        f"{summary.rows} rows from {summary.first} to {summary.last}; "
        f"{usd(summary.spending)} counted as spending, {summary.ignored} ignored "
        "(payments and transfers)."
    )
    print("Written to the ledger." if summary.applied else "Dry run only. Add --apply to save.")


if __name__ == "__main__":
    main()
