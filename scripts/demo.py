"""Show the morning report and a few command answers using synthetic data. No AWS, no AI.

python scripts/demo.py
python scripts/demo.py --today 2026-10-14 --ask "dining" --ask "summary"
"""

import argparse
from datetime import UTC, date, datetime

from budget_bot.budget import HOUSEHOLD_TIME, BudgetService
from budget_bot.commands import command_reply
from budget_bot.fixtures import FixtureProvider
from budget_bot.ledger_memory import MemoryLedger
from budget_bot.report import build_report
from budget_bot.sync import run_sync


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--today", type=date.fromisoformat, default=datetime.now(HOUSEHOLD_TIME).date()
    )
    parser.add_argument("--ask", action="append", default=["help"])
    args = parser.parse_args()
    now = datetime(args.today.year, args.today.month, args.today.day, 8, tzinfo=HOUSEHOLD_TIME)
    ledger = MemoryLedger()
    run_sync(FixtureProvider(args.today), ledger, ledger, now.astimezone(UTC))
    service = BudgetService(ledger, ledger, lambda: now)
    print("=== 8 AM post (synthetic data) ===")
    print(build_report(service))
    for question in args.ask:
        print(f"\n=== /ask {question} ===")
        print(command_reply(question, service) or "(this one would go to the AI)")


if __name__ == "__main__":
    main()
