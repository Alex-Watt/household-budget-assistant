"""Plain-language commands answered without AI: exact, free and available if AI is off or down."""

import re

from budget_bot.budget import BudgetService
from budget_bot.categories import find_category
from budget_bot.money import usd
from budget_bot.render import NOT_CONNECTED, category_text, coverage_text, summary_text

HELP = (
    "Ask me about household card spending with `/ask`. Without AI I understand:\n"
    "- `summary`: the month so far against the month's budget\n"
    "- a category name such as `dining`, `groceries` or `shopping`: its limit and what is left\n"
    "- `coverage`: how fresh the card data is\n"
    "- `repeats`: charges that show up most months for a similar amount\n"
    'When AI answers are on, you can also ask things like "what did we spend at Amazon last '
    'week?", "move that Target charge to gifts", "raise dining to $900 this month", '
    '"move $100 from shopping to groceries this month", "the Ruth\'s Chris charge was really '
    '$100, someone paid us back $100", "always count WGC* as gifts" or "dining is $900 every '
    'month from now on".'
)
SUMMARY_WORDS = frozenset({"summary", "spent", "spending", "month", "overview", "total", "status"})
COVERAGE_WORDS = frozenset({"coverage", "sync", "freshness", "data", "connected"})
REPEAT_WORDS = frozenset({"repeats", "repeat", "recurring", "subscriptions", "subscription"})
FILLER = frozenset(
    "how much have has we is are the for on my our do does what whats s check show me tell "
    "budget left remaining so far this".split()
)
NOT_UNDERSTOOD = (
    "I can only answer simple questions without AI. Try `summary` or a category name, "
    "or `help` to see what I can do."
)


def _words(text: str) -> list[str]:
    return re.sub(r"[^a-z0-9 ]", " ", text.lower()).split()


def command_reply(text: str, service: BudgetService) -> str | None:
    """Reply to a recognized command, or None when the question needs the AI path."""
    words = _words(text)
    core = [word for word in words if word not in FILLER]
    if not words or core in (["help"], ["commands"]):
        return HELP
    phrase = " ".join(core)
    category = find_category(phrase, exact=True) if core else None
    summary_request = not core or set(core) <= SUMMARY_WORDS
    if not (
        category or summary_request or set(core) <= COVERAGE_WORDS or set(core) <= REPEAT_WORDS
    ):
        return None
    coverage = service.coverage()
    if set(core) <= REPEAT_WORDS and core:
        rows = service.repeating_charges()
        if not rows:
            return "No charge shows up in two of the last four months for a similar amount."
        lines = [
            f"- {name}: {usd(amount)} in {count} of the last four months"
            for name, amount, count in rows
        ]
        return "These charges repeat:\n" + "\n".join(lines)
    if not coverage.connected:
        return NOT_CONNECTED
    if category:
        return category_text(service.category_detail(category), coverage)
    if summary_request:
        return summary_text(service.month_summary(), coverage)
    return coverage_text(coverage)
