import argparse
from datetime import UTC, date, datetime
from typing import Any

import boto3
import pytest
from moto import mock_aws

from budget_bot.categories import Category
from budget_bot.ledger import DynamoLedger
from budget_bot.ports import FeedStatus
from scripts.ledger_admin import purge_feed, run
from tests.helpers import CHASE, MORNING, txn
from tests.integration.test_ledger import create_ledger_table

pytestmark = pytest.mark.integration

GROCERY = {"primary": "FOOD_AND_DRINK", "detailed": "FOOD_AND_DRINK_GROCERIES"}


def args(command: str, **values: str) -> argparse.Namespace:
    return argparse.Namespace(
        command=command, text=values.get("text", ""), feed=values.get("feed", "")
    )


def seeded() -> tuple[Any, DynamoLedger]:
    table = create_ledger_table(boto3.resource("dynamodb", region_name="us-east-2"))
    ledger = DynamoLedger(table)
    ledger.upsert(
        [
            txn("sandbox-1", "10.00", date(2026, 10, 3), feed="plaid:sandbox", **GROCERY),
            txn("sandbox-2", "20.00", date(2026, 10, 4), feed="plaid:sandbox", **GROCERY),
            txn("real-1", "30.00", date(2026, 10, 5), feed="plaid:real", **GROCERY),
        ]
    )
    for feed in ("plaid:sandbox", "plaid:real"):
        ledger.save_cursor(feed, "c")
        ledger.save_status(FeedStatus(feed, MORNING.isoformat(), None, (CHASE,)))
    return table, ledger


def test_purge_removes_one_feed_and_leaves_the_rest() -> None:
    with mock_aws():
        table, ledger = seeded()
        assert purge_feed(table, "plaid:sandbox") == 2
        assert ledger.get("sandbox-1") is None and ledger.get("real-1") is not None
        assert ledger.cursor("plaid:sandbox") is None and ledger.cursor("plaid:real") == "c"
        assert [s.feed for s in ledger.statuses()] == ["plaid:real"]


@pytest.mark.parametrize("feed", ["", "sandbox"])
def test_purge_refuses_a_malformed_feed(feed: str) -> None:
    with mock_aws():
        table, _ = seeded()
        with pytest.raises(ValueError):
            purge_feed(table, feed)


def test_purge_needs_the_feed_name_typed_back() -> None:
    with mock_aws():
        table, ledger = seeded()
        said: list[str] = []
        run(args("purge", feed="plaid:sandbox"), table, lambda _p: "yes", said.append)
        assert said == ["Cancelled."] and ledger.get("sandbox-1") is not None
        run(args("purge", feed="plaid:sandbox"), table, lambda _p: "plaid:sandbox", said.append)
        assert "Removed 2 transactions" in said[-1] and ledger.get("sandbox-1") is None


def test_report_and_ask_read_the_real_ledger_without_posting() -> None:
    with mock_aws():
        table, _ = seeded()
        said: list[str] = []
        run(args("report"), table, out=said.append, now=lambda: MORNING)
        assert "**$60.00** of $6,280.00 (1%)" in said[0]
        run(args("ask", text="groceries"), table, out=said.append, now=lambda: MORNING)
        assert said[1].startswith("Groceries and household: $60.00 of $1,300.00 (5%)")
        run(args("ask", text="why did dining jump?"), table, out=said.append, now=lambda: MORNING)
        assert said[2] == "That question needs the AI."


def test_audit_lists_corrections_newest_first() -> None:
    with mock_aws():
        table, ledger = seeded()
        from budget_bot.budget import BudgetService

        service = BudgetService(ledger, ledger, lambda: datetime(2026, 10, 14, 13, tzinfo=UTC))
        service.recategorize("real-1", Category.GIFTS, "100000000000000004")
        said: list[str] = []
        run(args("audit"), table, out=said.append)
        assert said == [
            "2026-10-14T13:00:00+00:00  user 100000000000000004  recategorize  "
            "groceries_household -> gifts_donations"
        ]
