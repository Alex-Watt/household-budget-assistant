import pytest

from budget_bot.commands import HELP, NOT_UNDERSTOOD, command_reply
from budget_bot.render import NOT_CONNECTED
from tests.helpers import budget_service, txn

GROCERY = {"primary": "FOOD_AND_DRINK", "detailed": "FOOD_AND_DRINK_GROCERIES"}


@pytest.mark.parametrize("text", ["help", "Help", "?", "", "  ", "commands", "help me"])
def test_help(text: str) -> None:
    assert command_reply(text, budget_service([])) == HELP


@pytest.mark.parametrize(
    "text",
    [
        "summary",
        "Summary?",
        "How much have we spent this month?",
        "how much have we spent",
        "status",
        "budget",
    ],
)
def test_summary_questions_are_answered_without_ai(text: str) -> None:
    reply = command_reply(text, budget_service([txn("a", "120.00", **GROCERY)]))
    assert reply is not None
    assert reply.startswith("Month to date: $120.00 of $6,280.00 (2%) spent")
    assert "- Groceries and household: $120.00 of $1,300.00 (9%)" in reply


@pytest.mark.parametrize(
    "text",
    ["groceries", "How much groceries budget is left?", "Groceries and household", "grocery"],
)
def test_category_questions_are_answered_without_ai(text: str) -> None:
    reply = command_reply(text, budget_service([txn("a", "120.00", **GROCERY)]))
    assert reply is not None
    expected = "Groceries and household: $120.00 of $1,300.00 (9%) \u2014 $1,180.00 left"
    assert reply.startswith(expected)


@pytest.mark.parametrize(
    "text",
    [
        "What did we spend at Amazon last week?",
        "Why did dining jump this month?",
        "Move that Target charge to Gifts",
        "Can we afford $200 on clothes?",
        "dining and shopping",
        "How is shopping this week",
    ],
)
def test_anything_richer_is_left_for_the_ai(text: str) -> None:
    assert command_reply(text, budget_service([txn("a", "1.00")])) is None


def test_coverage_command() -> None:
    reply = command_reply("coverage", budget_service([]))
    assert reply is not None and reply.startswith("Data through Oct 14, 8:00 AM CT")


def test_commands_say_cards_are_not_connected_before_any_sync() -> None:
    service = budget_service([], synced=False)
    assert command_reply("summary", service) == NOT_CONNECTED
    assert command_reply("dining", service) == NOT_CONNECTED
    # Help needs no data.
    assert command_reply("help", service) == HELP


def test_unhelpful_text_constant_points_to_commands() -> None:
    assert "summary" in NOT_UNDERSTOOD and "help" in NOT_UNDERSTOOD
