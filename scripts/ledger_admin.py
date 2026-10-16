"""Inspect and clean the ledger from this PC (needs `aws login`). Nothing posts to Discord.

    python scripts/ledger_admin.py report                  # the 8 AM message as it would read now
    python scripts/ledger_admin.py ask "dining"            # a command answer, no AI
    python scripts/ledger_admin.py audit                   # recent category corrections
    python scripts/ledger_admin.py purge --feed plaid:<item id>   # delete one feed's data

The table name comes from --table or the LEDGER_TABLE environment variable (stack output
LedgerTableName). `purge` is for removing sandbox rehearsal data or an unlinked card; it asks
for confirmation first.
"""

import argparse
import os
from collections.abc import Callable
from datetime import datetime
from typing import Any

from budget_bot.aws_login import sign_in_or_exit
from budget_bot.budget import HOUSEHOLD_TIME, BudgetService
from budget_bot.commands import command_reply
from budget_bot.ledger import DynamoLedger, ledger_table
from budget_bot.report import build_report


def purge_feed(table: Any, feed: str) -> int:
    return DynamoLedger(table).purge_feed(feed)


def run(
    args: argparse.Namespace,
    table: Any,
    confirm: Callable[[str], str] = input,
    out: Callable[[str], None] = print,
    now: Callable[[], datetime] = lambda: datetime.now(HOUSEHOLD_TIME),
) -> None:
    ledger = DynamoLedger(table)
    service = BudgetService(ledger, ledger, now)
    if args.command == "report":
        out(build_report(service))
    elif args.command == "ask":
        out(command_reply(args.text, service) or "That question needs the AI.")
    elif args.command == "audit":
        for entry in ledger.audit_log(20):
            out(
                f"{entry.at}  user {entry.user_id}  {entry.action}  {entry.from_category} -> {entry.to_category}"
            )
    elif confirm(f"Delete ALL data for {args.feed}? Type the feed name to confirm: ") == args.feed:
        out(
            f"Removed {purge_feed(table, args.feed)} transactions and the feed's cursor and status."
        )
    else:
        out("Cancelled.")


def main() -> None:
    parser = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawTextHelpFormatter
    )
    parser.add_argument("command", choices=["report", "ask", "audit", "purge"])
    parser.add_argument("text", nargs="?", default="")
    parser.add_argument("--table", default=os.environ.get("LEDGER_TABLE", ""))
    parser.add_argument("--feed", default="")
    args = parser.parse_args()
    if not args.table:
        raise SystemExit("Pass --table or set LEDGER_TABLE (the LedgerTableName stack output).")
    sign_in_or_exit()
    run(args, ledger_table(args.table))


if __name__ == "__main__":
    main()
