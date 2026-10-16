"""The restricted tools the model may call. Each wraps a deterministic budget function.

Arguments are untrusted model output and are validated here. Results are JSON whose money
values are already formatted strings ("$1,234.56"), so the model quotes them and never adds,
rounds or converts. Transaction text is untrusted data and is bounded and cleaned upstream.
"""

import calendar
import json
import re
import unicodedata
from collections.abc import Callable
from datetime import date
from decimal import Decimal, InvalidOperation
from typing import Any

from budget_bot.accounts import account_label
from budget_bot.budget import (
    MAX_LIMIT_CHANGES,
    PERIODS,
    BudgetChange,
    BudgetError,
    BudgetService,
    Coverage,
    LimitChange,
    MonthSummary,
    RecategorizeError,
    SearchRow,
    resolve_period,
)
from budget_bot.categories import CATEGORIES, Category, label, mentions_category
from budget_bot.changes import (
    AdjustmentResult,
    RuleResult,
    adjust_charge,
    change_defaults,
    clean_match,
    newest_budget_change,
    remove_rule,
    set_rule,
    split_charge,
    undo_adjustment,
    undo_defaults,
    undo_rule_change,
    undo_split,
)
from budget_bot.money import MAX_AMOUNT, to_cents, usd
from budget_bot.render import pct_text, when
from budget_bot.transactions import normalize_match, search_words

# A change is only allowed when the person's own words give an instruction: the question has to
# open with a request verb (optionally after "please" or "can you"), contain no negation, and,
# for a move, name the target category. A merchant name or any other text a tool returns can
# therefore never trigger a correction by itself, and a question that merely mentions "change"
# ("why did dining change?") never enables one.
_LEAD = (
    r"(?:please\s+|pls\s+)?"
    r"(?:(?:can|could|would|will)\s+you\s+(?:please\s+)?|i\s+(?:want|need|would\s+like)\s+you\s+to\s+"
    r"|i'd\s+like\s+you\s+to\s+|go\s+ahead\s+and\s+|let'?s\s+)?"
)
_VERB = (
    r"(?:move|recategori[sz]e|re-?categori[sz]e|reclassify|change|switch|put|count|"
    r"categori[sz]e|undo|revert)"
)
CHANGE_REQUEST = re.compile(rf"\s*{_LEAD}{_VERB}\b", re.IGNORECASE)
NEGATION = re.compile(r"\b(?:do\s+not|don'?t|never|without|stop|cancel|no\s+need)\b", re.IGNORECASE)


def _plain(question: str) -> str:
    """Typographic apostrophes as plain ones, so "don't" and "we'd" match however they are typed."""
    return re.sub("[\u2018\u2019\u02bc\u2032]", "'", question)


def instruction(question: str) -> str:
    """The opening clause: the request itself, without a later explanation or question."""
    return _INSTRUCTION_END.split(_plain(question), maxsplit=1)[0].strip()


def looks_like_a_question(question: str) -> bool:
    """A question or an aside that starts one. A polite "can you ...?" is still a request.

    "is" and "was" inside an explanation ("undo that, the budget is fine") are not questions.
    """
    asked = _plain(question)
    if "?" in asked and not _POLITE_REQUEST.match(asked):
        return True
    rest = asked[len(instruction(question)) :]
    return bool(
        re.search(
            r"(?:^|\s)(?:why|what|how|when|where|who|which|did|do|does|should)\b",
            rest,
            re.IGNORECASE,
        )
    )


def wants_change(question: str) -> bool:
    asked = instruction(question)
    return (
        bool(CHANGE_REQUEST.match(asked))
        and not NEGATION.search(asked)
        and not looks_like_a_question(question)
    )


# Changing a budget limit has its own, stricter gate. It has to open with a request ("raise
# dining to $900", "can we move $100 from shopping to groceries?") and not be advice-seeking
# ("should we...?"). It also needs a number, and the tool then insists that every dollar
# amount, category, month and direction matches what the person actually wrote.
_BUDGET_LEAD = (
    r"(?:please\s+|pls\s+)?"
    r"(?:(?:can|could)\s+(?:you|we|i)\s+(?:please\s+)?|(?:would|will)\s+you\s+(?:please\s+)?"
    r"|(?:i|we)\s+(?:want|need|would\s+like)\s+(?:you\s+)?to\s+"
    r"|(?:i|we)'d\s+like\s+(?:you\s+)?to\s+|go\s+ahead\s+and\s+|let'?s\s+)?"
)
_BUDGET_VERB = (
    r"(?:raise|lower|increase|decrease|reduce|cut|trim|bump|boost|set|adjust|update|change|make|"
    r"move|shift|transfer|give|add|take|put)"
)
BUDGET_REQUEST = re.compile(rf"\s*{_BUDGET_LEAD}{_BUDGET_VERB}\b", re.IGNORECASE)
UNDO_REQUEST = re.compile(rf"\s*{_BUDGET_LEAD}(?:undo|revert|reverse|roll\s*back)\b", re.IGNORECASE)
BUDGET_WORD = re.compile(r"\b(?:budget|limit|cap|allowance|default|standing)s?\b", re.IGNORECASE)
TRANSACTION_WORD = re.compile(r"\b(?:transactions?|charges?|purchases?|payments?)\b", re.IGNORECASE)
_RAISE = re.compile(r"\b(?:raise|increase|bump|boost|add|extra|more|up)\b", re.IGNORECASE)
_LOWER = re.compile(r"\b(?:lower|decrease|reduce|cut|trim|less|drop|shrink|fewer)\b", re.IGNORECASE)
# "from shopping to groceries": which side each category is on decides which way it moves.
_MOVE = re.compile(
    r"\bfrom\b(?P<source>.+?)\b(?:to|into|toward|towards|for)\b(?P<target>.+)",
    re.IGNORECASE | re.DOTALL,
)
_MONTHS = {name.lower(): number for number, name in enumerate(calendar.month_name) if name}
_MONTHS |= {name.lower(): number for number, name in enumerate(calendar.month_abbr) if name}
_SURE_MONTH = re.compile(
    r"\b(january|february|april|june|july|august|september|october|november|december)\b",
    re.IGNORECASE,
)
_MONTH_PHRASE = re.compile(
    r"\b(?:for|in|during|of|until|through)\s+(?:the\s+month\s+of\s+)?(?P<a>[a-z]{3,9})\b"
    r"|\b(?P<b>[a-z]{3,9})\s+(?:budgets?|limits?)\b",
    re.IGNORECASE,
)
# Dates are not dollar amounts, so they are blanked out before numbers are read.
_DATE = re.compile(r"\b\d{4}-\d{1,2}(?:-\d{1,2})?\b|\b\d{1,2}/\d{1,2}(?:/\d{2,4})?\b")
# "30 days" is a span of time, not a dollar amount. Blanked the same way.
_DURATION = re.compile(
    r"\b\d[\d,]*(?:\.\d+)?\s*(?:days?|weeks?|months?|years?|hours?)\b", re.IGNORECASE
)
# The instruction is the opening clause. A colon, semicolon, dash or question mark starts an
# explanation or a question, and that part cannot authorize a write. A comma does too, except
# the comma inside a number ("$1,800").
_INSTRUCTION_END = re.compile(r"[:;]|\s[-\u2014]\s|\?|(?<!\d),(?!\d)")
# "Can you ...?" is a request. A bare "?" ("Undo?") is a question.
_POLITE_REQUEST = re.compile(
    r"\s*(?:please\s+|pls\s+)?(?:(?:can|could|would|will)\s+(?:you|we|i)\b|(?:i|we)\s+"
    r"(?:want|need|would\s+like)\b|(?:i|we)'d\s+like\b)",
    re.IGNORECASE,
)
# A dollar figure as people write it: "$1,800", "1800", "1.5k", optionally after "to" or "by".
_NUMBER = re.compile(
    r"(?:\b(?P<word>to|by|at)\s+)?(?<![\w.])\$?\s?"
    r"(?P<number>\d{1,3}(?:,\d{3})+(?:\.\d+)?|\d+(?:\.\d+)?)(?P<thousands>k(?![\w-]))?(?!\w)",
    re.IGNORECASE,
)


def _stated(question: str) -> dict[Decimal, set[str]]:
    """Every number the person wrote (in cents), with the "to"/"by" that came just before it."""
    text = _DURATION.sub(" ", _DATE.sub(" ", unicodedata.normalize("NFKC", question)))
    found: dict[Decimal, set[str]] = {}
    for match in _NUMBER.finditer(text):
        try:
            value = Decimal(match["number"].replace(",", "")) * (1000 if match["thousands"] else 1)
            amount = to_cents(value)
        except InvalidOperation, ValueError:
            continue
        words = found.setdefault(amount, set())
        words.add((match["word"] or "").lower())
        if "$" in match.group(0):
            words.add("$")
    return found


def question_amounts(question: str) -> set[Decimal]:
    """The dollar figures a person wrote, so a tool can insist its amount is one of them."""
    return set(_stated(question))


def wants_budget_change(question: str) -> bool:
    asked = instruction(question)
    # The amount may follow a comma ("this month, to $800") and still be part of the request.
    amounts = _stated(_plain(question))
    # A bare number ("30 days", "is 750 right") is not a new limit. The amount has to be
    # written as money: with a $, or right after "to" or "by".
    anchored = any(words & {"to", "by", "$"} for words in amounts.values())
    return (
        bool(BUDGET_REQUEST.match(asked))
        and not NEGATION.search(asked)
        and not looks_like_a_question(question)
        and anchored
    )


def wants_budget_undo(question: str) -> bool:
    return undo_kind(question) == "budget"


# Charge adjustments, merchant rules and standing limits have their own gates in the same spirit:
# the person has to say it as a statement or request (not ask a question), and the tool then
# checks every number and name against what they wrote.
_ASKS = re.compile(
    r"\s*(?:why|what|how|when|where|who|which|is|are|was|were|do|does|did|should|has|have"
    r"|(?:can|could|would|will)(?!\s+(?:you|we|i)\b))\b",
    re.IGNORECASE,
)
ADJUST_REQUEST = re.compile(
    rf"\s*{_BUDGET_LEAD}(?:adjust|count|treat|record|change|set|make|correct|update|mark)\b",
    re.IGNORECASE,
)
ADJUST_WORDS = re.compile(
    r"\b(?:really|actually|reimburs\w*|refunded|only\s+(?:paid|cost|counts?)"
    r"|(?:paid|pay|gave|give|gives|sent|send|got|get)\s+(?:us\s+|me\s+)?(?:\$?\d[\d,.]*\s+)?back"
    r"|(?:paid|gave|gives?)\s+us|counts?\s+(?:it\s+)?as|should\s+(?:only\s+)?(?:be|count|cost))\b",
    re.IGNORECASE,
)
STANDING = re.compile(
    r"\b(?:always|every\s+time|each\s+time|whenever|from\s+now\s+on|going\s+forward|"
    r"in\s+future|all\s+future|permanent(?:ly)?|every\s+month|each\s+month)\b",
    re.IGNORECASE,
)
RULE_WORD = re.compile(r"\brules?\b", re.IGNORECASE)
REMOVE_RULE = re.compile(rf"\s*{_BUDGET_LEAD}(?:remove|delete|drop|forget|clear)\b", re.IGNORECASE)
ADJUSTMENT_WORD = re.compile(
    r"\b(?:adjust\w*|reimburs\w*|counted|counts?\s+as|paid\s+back)\b", re.IGNORECASE
)
# How much it counts for ("count it as $100", "change it to $100").
AMOUNT_MARKER = re.compile(r"\b(?:as|to|at|for)\s+\$?\d", re.IGNORECASE)
# Words that make a remark a doubt, a hypothetical or a denial, never a statement of fact.
DOUBT = re.compile(
    r"\b(?:if|whether|maybe|perhaps|suppose|supposing|might|would|could|unless|not|never"
    r"|wasn'?t|isn'?t|weren'?t|aren'?t)\b",
    re.IGNORECASE,
)
RULE_REQUEST = re.compile(
    rf"\s*{_BUDGET_LEAD}(?:always|from\s+now\s+on|every\s+time|whenever|going\s+forward|count|treat"
    r"|put|file|categori[sz]e|classify|make|set|move|mark|record)\b",
    re.IGNORECASE,
)
# What the person said about a charge: money paid back, or what it really counts for.
PAID_BACK = re.compile(
    r"\b(?:reimburs\w*|(?:paid|pay|gave|give|sent|send|got|get)\s+(?:us\s+|me\s+)?(?:\$?[\d,.]+\s+)?back"
    r"|(?:paid|gave|gives?)\s+us|refunded|covered)\b",
    re.IGNORECASE,
)
COUNTS_AS = re.compile(
    r"\b(?:really|actually|only|counts?\s+(?:it\s+)?as|should\s+(?:only\s+)?(?:be|count|cost))\b"
    r"|\b(?:as|to|at)\s+\$?\d",  # "as $100": no word boundary after the first digit
    re.IGNORECASE,
)
FULL = re.compile(
    r"\b(?:all|full|fully|entire|entirely|whole|completely|nothing|zero)\b", re.IGNORECASE
)
# Words that appear in nearly any sentence about money, so finding a charge with one of them
# says nothing about which charge the person meant.
GENERIC_WORDS = frozenset(
    "the and for that this our was one charge charges purchase purchases payment payments transaction transactions".split()
)
# Where an undo request's subject ends: "undo that correction, it counted as groceries".
_CLAUSE_END = re.compile(
    r"[,.;:!?]|\s[-\u2014]\s|\b(?:because|since|but|so|which|it|they)\b", re.IGNORECASE
)


def wants_adjustment(question: str) -> bool:
    """Said a charge counts for less ("that charge was really $100", "count it as $100")."""
    clause = instruction(question)
    # The reason often follows a comma ("really $100, someone paid us back $100").
    asked = _plain(question)
    if NEGATION.search(clause) or looks_like_a_question(question) or not question_amounts(asked):
        return False
    if ADJUST_REQUEST.match(clause):
        # A request must also say what it counts for ("as $100", "to $100") or why.
        return bool(AMOUNT_MARKER.search(asked) or ADJUST_WORDS.search(asked))
    # A statement is never a question, a doubt, a hypothetical or a denial.
    # "was split, someone reimbursed us" keeps the reason that follows the comma.
    return not _ASKS.match(clause) and not DOUBT.search(clause) and bool(ADJUST_WORDS.search(asked))


def wants_rule(question: str) -> bool:
    """A standing instruction ("always count WGC* as gifts"), not a question about one."""
    asked = instruction(question)
    return (
        bool(RULE_REQUEST.match(asked))
        and bool(STANDING.search(asked))
        and not NEGATION.search(asked)
        and not looks_like_a_question(question)
    )


def wants_rule_removal(question: str) -> bool:
    asked = instruction(question)
    return (
        bool(REMOVE_RULE.match(asked))
        and bool(RULE_WORD.search(asked))
        and not NEGATION.search(asked)
        and not looks_like_a_question(question)
    )


def undo_kind(question: str) -> str | None:
    """What an undo request is about: a merchant "rule", a "budget" change, a charge
    "adjustment", or, when it says nothing more, a category "correction". Earlier kinds win, so
    "undo the budget adjustment" is about the budget."""
    if looks_like_a_question(question):
        return None
    asked = instruction(question)
    matched = UNDO_REQUEST.match(asked)
    if not matched or NEGATION.search(asked):
        return None
    # Only the words right after the verb say what is being undone, not a later explanation.
    about = _CLAUSE_END.split(asked[matched.end() :], maxsplit=1)[0]
    if RULE_WORD.search(about):
        return "rule"
    if BUDGET_WORD.search(about):
        return "budget"
    if ADJUSTMENT_WORD.search(about):
        return "adjustment"
    if re.search(r"\bsplits?\b", about, re.IGNORECASE):
        return "split"
    return "correction"


def listed_charge(question: str, charges: list[dict[str, Any]]) -> dict[str, Any] | None:
    """The one charge a follow-up names by its amount, its date, or "the first" / "the second"."""
    if not charges:
        return None
    amounts = question_amounts(question)
    by_amount = [charge for charge in charges if to_cents(charge["amount_value"]) in amounts]
    if len(by_amount) == 1:
        return by_amount[0]
    text = _plain(question).lower()
    by_date: list[dict[str, Any]] = []
    for charge in charges:
        day = date.fromisoformat(str(charge["date"]))
        month = calendar.month_name[day.month].lower()
        if day.isoformat() in text or f"{month} {day.day}" in text:
            by_date.append(charge)
    if len(by_date) == 1:
        return by_date[0]
    if re.search(r"\b(?:first|1st)\b", text):
        return charges[0]
    if re.search(r"\b(?:second|2nd)\b", text) and len(charges) > 1:
        return charges[1]
    return None


def choice_reply(charges: list[dict[str, Any]]) -> str:
    lines = [
        f"- {charge['merchant']}, {charge['amount']}, on {charge['date']}" for charge in charges
    ]
    return (
        "More than one charge matches, so no change was made. "
        "Reply with the date or the amount:\n" + "\n".join(lines)
    )


SPLIT_WORD = re.compile(r"\bsplit\b", re.IGNORECASE)


def wants_split(question: str) -> bool:
    asked = instruction(question)
    return (
        bool(SPLIT_WORD.search(asked))
        and not NEGATION.search(asked)
        and not looks_like_a_question(question)
    )


def _direction(question: str) -> int:
    """+1 if the person only asked to raise, -1 if only to lower, 0 if both or neither."""
    raising, lowering = bool(_RAISE.search(question)), bool(_LOWER.search(question))
    return 1 if raising and not lowering else -1 if lowering and not raising else 0


def requested_month(question: str, today: date) -> tuple[int, int]:
    """The month the person's words point at: this month unless they say next month or name one."""
    text = _plain(question).lower()
    this = (today.year, today.month)
    following = (today.year + (today.month == 12), today.month % 12 + 1)
    found: set[tuple[int, int]] = set()
    if re.search(r"\bnext\s+month\b", text):
        found.add(following)
    if re.search(r"\b(?:this|current)\s+month\b", text):
        found.add(this)
    for match in re.finditer(r"\b(\d{4})-(\d{2})\b", text):
        found.add((int(match[1]), int(match[2])))
    # Month names that cannot be anything else count wherever they appear. "May", "March" and
    # the three-letter forms are also ordinary words, so they count only after "for", "in" and
    # the like, or before "budget" or "limit".
    named = [_MONTHS[match[1].lower()] for match in _SURE_MONTH.finditer(text)]
    named += [
        _MONTHS[word]
        for match in _MONTH_PHRASE.finditer(text)
        if (word := (match["a"] or match["b"] or "").lower()) in _MONTHS
    ]
    for number in named:
        found.add(next(((y, m) for y, m in (this, following) if m == number), (0, number)))
    if not found <= {this, following}:
        raise ToolError("only this month's or next month's budget can be changed")
    if len(found) > 1:
        raise ToolError("name one month, this month or next month, in each request")
    return found.pop() if found else this


MONTH = re.compile(r"(\d{4})-(\d{2})\Z")
AFFORD_NOTE = (
    "This only shows room in the card budget. Whether it is truly affordable depends on "
    "current cash and unpaid bills, which this bot cannot see."
)
MAX_ROWS = 15
# Result fields that only echo what the model asked about, so they cannot ground an answer.
MODEL_SUPPLIED = frozenset({"purchase"})
CATEGORY_IDS = [info.category.value for info in CATEGORIES]
CATEGORY_KEY = "; ".join(f"{info.category.value}={info.label}" for info in CATEGORIES)


class ToolError(ValueError):
    """A request the tool refuses; the message goes back to the model."""


def _spec(name: str, description: str, properties: dict[str, Any], required: list[str]) -> Any:
    schema = {"type": "object", "properties": properties, "required": required}
    return {"toolSpec": {"name": name, "description": description, "inputSchema": {"json": schema}}}


_CATEGORY = {"type": "string", "enum": CATEGORY_IDS, "description": CATEGORY_KEY}
_MONTH = {"type": "string", "description": "Month as YYYY-MM. Omit for the current month."}
TOOL_SPECS: list[dict[str, Any]] = [
    _spec(
        "month_summary",
        "Month-to-date card spending against that month's budget (the household can change a "
        "month's limits): totals, pending, refunds, pace, and every category's spent, limit and "
        "remaining. Refunds and returns are already subtracted from spent and added to remaining.",
        {"month": _MONTH},
        [],
    ),
    _spec(
        "category_status",
        "One category's spending, limit, remaining and pace, with its biggest merchants.",
        {"category": _CATEGORY, "month": _MONTH},
        ["category"],
    ),
    _spec(
        "search_transactions",
        "Find counted transactions by merchant text, category and period. Totals cover every "
        "match; only the first rows are listed. Give `period` OR start_date and end_date.",
        {
            "merchant": {"type": "string", "description": "Text the merchant name contains."},
            "category": _CATEGORY,
            "period": {"type": "string", "enum": list(PERIODS)},
            "start_date": {"type": "string", "description": "YYYY-MM-DD"},
            "end_date": {"type": "string", "description": "YYYY-MM-DD"},
            "limit": {"type": "integer", "minimum": 1, "maximum": MAX_ROWS},
        },
        [],
    ),
    _spec(
        "explain_category_change",
        "Why a category is higher or lower than last month: this month so far against the same "
        "days last month, and the merchants that account for the increase.",
        {"category": _CATEGORY, "month": _MONTH},
        ["category"],
    ),
    _spec(
        "check_purchase",
        "How a possible purchase fits the category limit and the overall budget.",
        {"category": _CATEGORY, "amount": {"type": "number", "description": "US dollars"}},
        ["category", "amount"],
    ),
    _spec(
        "data_coverage",
        "Which cards are connected and how recent and complete the data is.",
        {},
        [],
    ),
    _spec(
        "recategorize_transaction",
        "Move ONE transaction to another category, only when the user explicitly asks. `ref` must "
        "come from search_transactions in this conversation. Reversible and audited.",
        {"ref": {"type": "string"}, "category": _CATEGORY},
        ["ref", "category"],
    ),
    _spec(
        "undo_last_recategorization",
        "Undo the most recent correction of one transaction's category, only when the user "
        "explicitly asks. Not for budget limits: use undo_last_budget_change for those.",
        {},
        [],
    ),
    _spec(
        "change_monthly_budget",
        "Change category LIMITS (the budget, not a transaction) for one month, this month or "
        "next only, and only when the user explicitly asks AND states the dollar amounts. Each "
        "change gives `new_limit` (set the limit to an amount) or `change_by` (add an amount; "
        "negative to subtract). To move money between categories send one negative and one "
        "positive change_by of the same amount. The tool does all arithmetic and returns the old "
        "and new limits and the new total. Reversible and audited. Set from_now_on true ONLY "
        'when the user says the change is permanent ("from now on", "every month", '
        '"always"): it then applies to every month from the chosen month onward; otherwise it '
        "is for that one month.",
        {
            "month": _MONTH,
            "from_now_on": {"type": "boolean"},
            "changes": {
                "type": "array",
                "minItems": 1,
                "maxItems": MAX_LIMIT_CHANGES,
                "items": {
                    "type": "object",
                    "properties": {
                        "category": _CATEGORY,
                        "new_limit": {"type": "number", "description": "US dollars"},
                        "change_by": {
                            "type": "number",
                            "description": "US dollars, may be negative",
                        },
                    },
                    "required": ["category"],
                },
            },
        },
        ["changes"],
    ),
    _spec(
        "undo_last_budget_change",
        "Undo the most recent change to category limits (a one-month change or a standing one, "
        "whichever is newest), only when the user explicitly asks to undo a budget or limit "
        "change.",
        {"month": _MONTH},
        [],
    ),
    _spec(
        "adjust_charge_amount",
        "Make ONE charge count for less than the card shows, for example a gift that someone paid "
        "part of back, only when the user explicitly says so and gives the dollar amount. Give "
        "`counted_amount` (what it really counts for) OR `reimbursed_amount` (how much was paid "
        "back). `ref` must come from search_transactions in this conversation. The card's own "
        "amount is kept. Reversible and audited.",
        {
            "ref": {"type": "string"},
            "counted_amount": {"type": "number", "description": "US dollars"},
            "reimbursed_amount": {"type": "number", "description": "US dollars"},
            "note": {
                "type": "string",
                "description": "A few words, such as 'gift, $100 paid back'",
            },
        },
        ["ref"],
    ),
    _spec(
        "undo_last_charge_adjustment",
        "Undo the most recent charge adjustment, only when the user explicitly asks to undo an "
        "adjustment.",
        {},
        [],
    ),
    _spec(
        "split_charge",
        "Count one posted receipt in two categories. `parts` is exactly two {category, amount} "
        "objects and the amounts must add up to what the charge counts for. Only when the user "
        "says to split a charge and names both categories and amounts.",
        {
            "ref": {"type": "string"},
            "parts": {
                "type": "array",
                "items": {
                    "type": "object",
                    "properties": {
                        "category": _CATEGORY,
                        "amount": {"type": "number"},
                    },
                    "required": ["category", "amount"],
                },
            },
        },
        ["ref", "parts"],
    ),
    _spec(
        "undo_last_split",
        "Undo the most recent receipt split, only when the user explicitly asks to undo a split.",
        {},
        [],
    ),
    _spec(
        "set_merchant_rule",
        "From now on count every charge whose merchant name contains `merchant` in `category`, "
        "only when the user explicitly says always, from now on or the like. It applies to past "
        "and future charges, but a person's correction of a single charge still wins. Reversible.",
        {
            "merchant": {
                "type": "string",
                "description": "Text the merchant name contains: 4 or more characters, in the "
                "user's own words.",
            },
            "category": _CATEGORY,
        },
        ["merchant", "category"],
    ),
    _spec(
        "remove_merchant_rule",
        "Remove a standing merchant rule, only when the user explicitly asks to.",
        {"merchant": {"type": "string", "description": "The text the rule matches."}},
        ["merchant"],
    ),
    _spec(
        "undo_last_rule_change",
        "Undo the most recent merchant rule change, only when the user explicitly asks to undo a "
        "rule change.",
        {},
        [],
    ),
]
TOOL_NAMES = frozenset(spec["toolSpec"]["name"] for spec in TOOL_SPECS)
WRITE_TOOLS = frozenset(
    {
        "recategorize_transaction",
        "undo_last_recategorization",
        "change_monthly_budget",
        "undo_last_budget_change",
        "adjust_charge_amount",
        "undo_last_charge_adjustment",
        "split_charge",
        "undo_last_split",
        "set_merchant_rule",
        "remove_merchant_rule",
        "undo_last_rule_change",
    }
)


_URL = re.compile(r"\b(?:https?://|www\.)\S+", re.IGNORECASE)
_MONEY_MARKS = re.compile(r"[$%]|\bUSD\b", re.IGNORECASE)


def plain_merchant(name: str) -> str:
    """Bank-supplied text made safe to show a model or a person: no links, no money marks.

    A merchant name is untrusted. Without a "$" or "%", a figure inside it can never be
    mistaken for one of the bot's own totals, and a link cannot be made clickable.
    """
    return _MONEY_MARKS.sub("", _URL.sub("(link removed)", name)).strip() or "unnamed merchant"


def _month_words(month: str) -> str:
    """ "2026-10" as "October 2026"."""
    try:
        year, number = (int(part) for part in month.split("-"))
        return f"{calendar.month_name[number]} {year}"
    except ValueError, IndexError:
        return "that month"


def write_notice(result: dict[str, Any]) -> str:
    """Plain words for a change that was made, built only from the tool's own result.

    This is what a person is told after any change. The model's own wording is never used for
    it: a change is reported exactly as it happened, even if the model's reply is wrong,
    cannot be verified, or never arrives (and the person asks again, changing it twice).
    """
    if result.get("split") and result.get("changed"):
        bits = ", ".join(f"{part['amount']} as {part['category']}" for part in result["split"])
        merchant = result.get("merchant") or "that charge"
        return f"Split {merchant} into {bits}. Ask me to undo the last split to put it back."
    if set(result) <= {"undone", "merchant"} and result.get("undone"):
        return f"Undid the split on {result.get('merchant') or 'that charge'}."
    if "changes" in result:  # a budget change or its undo, for one month or standing
        month = _month_words(str(result.get("month", "")))
        moves = "\n".join(f"- {c['category']}: {c['was']} to {c['now']}" for c in result["changes"])
        if result.get("standing"):
            total = (
                f"The monthly budget from then on is now {result['budget_now']} "
                f"(was {result['budget_was']})."
            )
            if result.get("undone"):
                return f"Undid the latest standing limit change (from {month}):\n{moves}\n{total}"
            if not result.get("changed"):
                return f"No change was needed: those limits already apply from {month} on."
            kept = "".join(
                f" {month}'s own limit still applies for {c['category']}: {c['limit']}."
                for c in result.get("still_applies", [])
            )
            return (
                f"From {month} on, these limits change every month:\n{moves}\n{total}{kept} "
                "Ask me to undo the last budget change to put it back."
            )
        total = f"The month's budget is now {result['budget_now']} (was {result['budget_was']})."
        if result.get("undone"):
            return f"Undid the last budget change for {month}:\n{moves}\n{total}"
        if not result.get("changed"):
            return f"No change was needed: the {month} limits were already at those amounts."
        return (
            f"Changed the {month} budget:\n{moves}\n{total} "
            "Ask me to undo the last budget change to put it back."
        )
    if "card_charge" in result:  # a charge adjustment or its undo
        merchant = result.get("merchant") or "that charge"
        card = result.get("card_charge") or "its charge"
        now, was = result.get("now_counted_as"), result.get("was_counted_as")
        if result.get("undone"):
            return f"Undid the adjustment on {merchant}: it counts for {now} again (card: {card})."
        if not result.get("changed"):
            return f"No change was needed: {merchant} already counts for {now}."
        return (
            f"Adjusted {merchant} on {result.get('date')}: the card shows {card} and it now "
            f"counts for {now} (it counted for {was}). "
            "Ask me to undo the last adjustment to put it back."
        )
    if "rule" in result:  # a merchant rule change or its undo
        match = result["rule"]
        before, after = result.get("rule_before"), result.get("rule_after")
        count = int(result.get("this_month_charges", 0))
        moved = result.get("this_month_total")
        effect = (
            f" This month that moves {count} charge{'s' if count != 1 else ''} totalling {moved}."
            if count
            else " No charge this month is affected."
        )
        if result.get("undone"):
            if after:
                return f'Undid the last merchant rule change: "{match}" counts as {after} again.'
            return f'Undid the last merchant rule change: there is no rule for "{match}" now.'
        if not result.get("changed"):
            return f'No change was needed: charges from "{match}" already count as {after}.'
        if after is None:
            return (
                f'Removed the rule for "{match}" (it counted charges as {before}).{effect} '
                "Earlier months change back too."
            )
        was = f" (it was {before})" if before else ""
        return (
            f'From now on, charges from "{match}" count as {after}{was}.{effect} '
            "Earlier months follow the rule too. "
            "Ask me to undo the last rule change to put it back."
        )
    merchant, amount = result.get("merchant"), result.get("amount")
    what = f"{merchant} ({amount})" if merchant and amount else "that transaction"
    if result.get("undone"):
        return f"Undid the last correction: {what} is back in {result.get('now_in')}."
    if not result.get("changed"):
        return f"No change was needed: that transaction was already in {result.get('to')}."
    return (
        f"Moved {what} on {result.get('date')} from {result.get('from')} to {result.get('to')}. "
        "Ask me to undo the last correction to put it back."
    )


def _json(value: dict[str, Any]) -> str:
    return json.dumps(value, ensure_ascii=False, separators=(",", ":"))


def summary_data(summary: MonthSummary, coverage: Coverage) -> dict[str, Any]:
    projected = summary.projected
    return {
        "data_available": coverage.connected,
        "month": f"{summary.year}-{summary.month:02d}",
        "as_of": summary.as_of.isoformat(),
        "budget": usd(summary.budget),
        "usual_budget": usd(summary.default_budget),
        "charges_counted_for_less_than_the_card_shows": summary.adjusted_count,
        "counted_for_less_by": usd(summary.adjusted_total),
        "limits_changed_this_month": bool(summary.limits_changed),
        "spent_including_pending": usd(summary.committed),
        "posted": usd(summary.posted),
        "pending": usd(summary.pending),
        "refunds": usd(summary.refunds),
        "remaining": usd(summary.remaining),
        "over_budget": summary.remaining < 0,
        "percent_used": pct_text(summary.committed, summary.budget),
        "days_left_including_today": summary.days_left,
        "percent_of_month_elapsed": f"{int(summary.elapsed * 100)}%",
        "pace": summary.pace,
        "projected_month_end": usd(projected) if projected is not None else None,
        "projected_over_budget_by": (
            usd(summary.projected_overrun) if summary.projected_overrun is not None else None
        ),
        "categories": [
            {
                "id": line.category.value,
                "name": label(line.category),
                "spent": usd(line.committed),
                "limit": usd(line.cap),
                **(
                    {"usual_limit": usd(line.usual)}
                    if line.category in summary.limits_changed and line.usual is not None
                    else {}
                ),
                "remaining": usd(line.remaining),
                "percent_used": pct_text(line.committed, line.cap),
                "over_budget": line.over,
            }
            for line in summary.lines
        ],
        "note": (
            "Card spending only. Card payments, rewards credits, paychecks, account transfers and "
            f"duplicate imports are left out ({summary.excluded_count} records ignored). Refunds "
            "and returns reduce spent and increase remaining, including when the bank labeled the "
            "return as a transfer or income."
        ),
    }


def coverage_data(coverage: Coverage) -> dict[str, Any]:
    return {
        "data_available": coverage.connected,
        "accounts_connected": [account_label(key) for key in coverage.connected_accounts],
        "accounts_not_connected": [account_label(key) for key in coverage.missing_accounts],
        "newest_data": when(coverage.newest_success) if coverage.newest_success else None,
        "problems": list(coverage.problems),
    }


def _row_data(ref: str, row: SearchRow) -> dict[str, Any]:
    return {
        "ref": ref,
        "date": row.spend_date.isoformat(),
        "merchant": plain_merchant(row.merchant),
        "amount": usd(row.amount),
        **(
            {"card_charge": usd(row.charged), "adjusted": "counted for less than the card shows"}
            if row.charged is not None
            else {}
        ),
        "category": label(row.category),
        "status": "pending" if row.pending else "posted",
    }


class ToolRunner:
    """Executes tool calls for one question. A new runner is made for every question."""

    def __init__(
        self, service: BudgetService, user_id: str, question: str, request_id: str = ""
    ) -> None:
        self.service = service
        self.user_id = user_id
        self.question = question
        # The Discord interaction. A redelivered request must not repeat a change it already made.
        self.request_id = request_id
        self.changes_allowed = wants_change(question)
        self.budget_changes_allowed = wants_budget_change(question)
        self.budget_undo_allowed = wants_budget_undo(question)
        self.adjustment_allowed = wants_adjustment(question)
        self.rule_allowed = wants_rule(question)
        self.rule_removal_allowed = wants_rule_removal(question)
        self.undo_kind = undo_kind(question)
        # Shared by every kind of write: one change per question, whatever it is.
        self.changes_made = 0
        self.write_attempted = False  # a write tool was called, even if it refused
        self.write_refusal: str | None = None  # why it refused, when that is safe to show
        self.write_notice: str | None = None  # plain words for the change that was made, if any
        self.refs: dict[str, str] = {}
        # What the model searched for to find each ref (the person's own word for the merchant)
        # and each charge's card amount, so a change can check it is the charge they named.
        self.ref_terms: dict[str, str] = {}
        self.ref_amounts: dict[str, Decimal] = {}
        self.ref_info: dict[str, dict[str, str]] = {}
        self.shown: list[dict[str, str]] = []
        self.choice_text: str | None = None
        self.pending_choice: dict[str, Any] | None = None
        self.split_allowed = wants_split(question)
        self.skipped_writes = 0  # changes refused only because one was already made
        # What the model was told, kept so its answer can be checked against it. Values the
        # model itself supplied (a purchase amount) are left out: they prove nothing.
        self.results: list[str] = []
        self._handlers: dict[str, Callable[[dict[str, Any]], dict[str, Any]]] = {
            "month_summary": self._month_summary,
            "category_status": self._category_status,
            "search_transactions": self._search_transactions,
            "explain_category_change": self._explain_category_change,
            "check_purchase": self._check_purchase,
            "data_coverage": self._data_coverage,
            "recategorize_transaction": self._recategorize_transaction,
            "undo_last_recategorization": self._undo_last_recategorization,
            "change_monthly_budget": self._change_monthly_budget,
            "undo_last_budget_change": self._undo_last_budget_change,
            "adjust_charge_amount": self._adjust_charge_amount,
            "undo_last_charge_adjustment": self._undo_last_charge_adjustment,
            "split_charge": self._split_charge,
            "undo_last_split": self._undo_last_split,
            "set_merchant_rule": self._set_merchant_rule,
            "remove_merchant_rule": self._remove_merchant_rule,
            "undo_last_rule_change": self._undo_last_rule_change,
        }

    def run(self, name: str, arguments: Any) -> str:
        handler = self._handlers.get(name)
        self.write_attempted = self.write_attempted or name in WRITE_TOOLS
        try:
            if handler is None or not isinstance(arguments, dict):
                raise ToolError("unknown tool or malformed arguments")
            result = handler(arguments)
        except (ValueError, RecategorizeError) as exc:
            result = {"error": str(exc)}
            # These two kinds of message are written to be shown to people, unlike the gate's.
            if name in WRITE_TOOLS and isinstance(exc, BudgetError | RecategorizeError):
                self.write_refusal = str(exc)
        self.results.append(_json({k: v for k, v in result.items() if k not in MODEL_SUPPLIED}))
        return _json(result)

    # Argument validation

    @staticmethod
    def _category(value: Any) -> Category:
        try:
            return Category(value)
        except ValueError as exc:
            raise ToolError("category must be one of the listed ids") from exc

    @staticmethod
    def _month(value: Any) -> tuple[int | None, int | None]:
        if value in (None, ""):
            return None, None
        match = MONTH.fullmatch(value) if isinstance(value, str) else None
        if not match or not 1 <= int(match[2]) <= 12:
            raise ToolError("month must look like 2026-10")
        return int(match[1]), int(match[2])

    @staticmethod
    def _date(value: Any) -> date:
        try:
            return date.fromisoformat(value)
        except (TypeError, ValueError) as exc:
            raise ToolError("dates must look like 2026-10-14") from exc

    @staticmethod
    def _amount(value: Any) -> Decimal:
        if isinstance(value, bool) or not isinstance(value, int | float | str):
            raise ToolError("amount must be a number")
        try:
            amount = Decimal(str(value))
        except InvalidOperation as exc:
            raise ToolError("amount must be a number") from exc
        if not amount.is_finite() or not 0 < amount <= MAX_AMOUNT:
            raise ToolError("amount must be positive and reasonable")
        return amount

    @classmethod
    def _signed_amount(cls, value: Any) -> Decimal:
        """Like _amount, but a negative number is allowed (a decrease)."""
        if isinstance(value, bool) or not isinstance(value, int | float | str):
            raise ToolError("amount must be a number")
        try:
            number = Decimal(str(value))
        except InvalidOperation as exc:
            raise ToolError("amount must be a number") from exc
        if not number.is_finite():
            raise ToolError("amount must be a number")
        return -cls._amount(str(-number)) if number < 0 else cls._amount(str(number))

    # Read-only tools

    def _month_summary(self, args: dict[str, Any]) -> dict[str, Any]:
        year, month = self._month(args.get("month"))
        return summary_data(self.service.month_summary(year, month), self.service.coverage())

    def _category_status(self, args: dict[str, Any]) -> dict[str, Any]:
        year, month = self._month(args.get("month"))
        detail = self.service.category_detail(self._category(args.get("category")), year, month)
        line, summary = detail.line, detail.summary
        return {
            "data_available": self.service.coverage().connected,
            "category": label(line.category),
            "month": f"{summary.year}-{summary.month:02d}",
            "spent_including_pending": usd(line.committed),
            "pending": usd(line.pending),
            "limit": usd(line.cap),
            "remaining": usd(line.remaining),
            "percent_used": pct_text(line.committed, line.cap),
            "over_budget": line.over,
            "days_left_including_today": summary.days_left,
            "percent_of_month_elapsed": f"{int(summary.elapsed * 100)}%",
            "overall_pace": summary.pace,
            "top_merchants": [
                {
                    "merchant": plain_merchant(m.merchant),
                    "total": usd(m.total),
                    "transactions": m.count,
                }
                for m in detail.top_merchants
            ],
        }

    def _search_transactions(self, args: dict[str, Any]) -> dict[str, Any]:
        today = self.service.today()
        if args.get("period") and (args.get("start_date") or args.get("end_date")):
            raise ToolError("give either period or start_date/end_date, not both")
        if args.get("period"):
            if args["period"] not in PERIODS:
                raise ToolError("unknown period")
            start, end = resolve_period(args["period"], today)
        elif args.get("start_date") and args.get("end_date"):
            start, end = self._date(args["start_date"]), self._date(args["end_date"])
        else:
            start, end = resolve_period("this_month", today)
        limit = args.get("limit", 8)
        if isinstance(limit, bool) or not isinstance(limit, int) or not 1 <= limit <= MAX_ROWS:
            raise ToolError(f"limit must be 1 to {MAX_ROWS}")
        category = self._category(args["category"]) if args.get("category") else None
        merchant = args.get("merchant")
        if merchant is not None and not isinstance(merchant, str):
            raise ToolError("merchant must be text")
        result = self.service.search(start, end, merchant or None, category, limit)
        rows = []
        for row in result.rows:
            ref = f"t{len(self.refs) + 1}"
            self.refs[ref] = row.txn_id
            self.ref_terms[ref] = merchant if isinstance(merchant, str) else ""
            self.ref_amounts[ref] = row.charged if row.charged is not None else row.amount
            info = {
                "txn_id": row.txn_id,
                "merchant": plain_merchant(row.merchant),
                "amount": usd(row.charged if row.charged is not None else row.amount),
                "amount_value": str(row.charged if row.charged is not None else row.amount),
                "date": row.spend_date.isoformat(),
            }
            self.ref_info[ref] = info
            if info not in self.shown:
                self.shown.append(info)
            rows.append(_row_data(ref, row))
        return {
            "start": start.isoformat(),
            "end": end.isoformat(),
            "match_count": result.count,
            "total_including_pending": usd(result.total),
            "posted_total": usd(result.posted),
            "pending_total": usd(result.pending),
            "listed": len(rows),
            "transactions": rows,
        }

    def _explain_category_change(self, args: dict[str, Any]) -> dict[str, Any]:
        year, month = self._month(args.get("month"))
        change = self.service.explain_change(self._category(args.get("category")), year, month)
        return {
            "category": label(change.category),
            "month": f"{change.year}-{change.month:02d}",
            "through_day_of_month": change.through_day,
            "this_month_so_far": usd(change.this_period),
            "same_days_last_month": usd(change.last_period),
            "difference": usd(change.delta),
            "last_full_month": usd(change.last_full_month),
            "biggest_increases": [
                {
                    "merchant": plain_merchant(item.merchant),
                    "this_month": usd(item.this_period),
                    "last_month_same_days": usd(item.last_period),
                    "increase": usd(item.delta),
                }
                for item in change.top_increases
            ],
        }

    def _check_purchase(self, args: dict[str, Any]) -> dict[str, Any]:
        category = self._category(args.get("category"))
        check = self.service.check_purchase(category, self._amount(args.get("amount")))
        return {
            "category": label(category),
            "purchase": usd(check.amount),
            "category_limit": usd(check.cap),
            "category_spent_so_far": usd(check.committed),
            "category_remaining_after": usd(check.category_after),
            "fits_category_limit": check.fits_category,
            "overall_remaining_after": usd(check.overall_after),
            "fits_overall_budget": check.fits_overall,
            "note": AFFORD_NOTE,
        }

    def _data_coverage(self, args: dict[str, Any]) -> dict[str, Any]:
        return coverage_data(self.service.coverage())

    # Guarded writes

    def _already_done(self) -> dict[str, Any] | None:
        """If a retry of this same request already made its change, report that, don't repeat it."""
        if not self.request_id:
            return None
        recalled = self.service.ledger.recall_write(self.request_id)
        if recalled is None:
            return None
        done: dict[str, Any] = json.loads(recalled)
        self.write_notice = write_notice(done)
        return {**done, "note": "This change was already made earlier for this same request."}

    def _allow_change(self) -> None:
        if not self.changes_allowed:
            raise ToolError("the user did not ask to change a category, so no change was made")
        if self.changes_made >= 1:
            self.skipped_writes += 1
            raise ToolError("only one change can be made per request")

    @property
    def write_gate_open(self) -> bool:
        """The person's words could have asked for a change (so "nothing changed" is news)."""
        return (
            self.budget_changes_allowed
            or self.budget_undo_allowed
            or self.adjustment_allowed
            or self.split_allowed
            or self.rule_allowed
            or self.rule_removal_allowed
            or self.undo_kind in {"rule", "adjustment", "split"}
        )

    def _allow_write(self, allowed: bool, what: str) -> None:
        if not allowed:
            raise ToolError(f"the user did not ask {what}, so no change was made")
        if self.changes_made >= 1:
            self.skipped_writes += 1
            raise ToolError("only one change can be made per request")

    def _allow_budget_change(self) -> None:
        if not self.budget_changes_allowed:
            raise ToolError("the user did not ask to change the budget, so no change was made")
        if self.changes_made >= 1:
            self.skipped_writes += 1
            raise ToolError("only one change can be made per request")

    def _finish_write(self, result: dict[str, Any]) -> dict[str, Any]:
        self.changes_made += 1
        self.write_notice = write_notice(result)
        if self.request_id:
            self.service.ledger.record_write(self.request_id, _json(result))
        return result

    def _recategorize_transaction(self, args: dict[str, Any]) -> dict[str, Any]:
        self._allow_change()
        if (earlier := self._already_done()) is not None:
            self.changes_made += 1
            return earlier
        txn_id = self.refs.get(str(args.get("ref")))
        if txn_id is None:
            raise ToolError("ref must come from search_transactions in this conversation")
        category = self._category(args.get("category"))
        if not mentions_category(instruction(self.question), category):
            raise ToolError("the user did not name that category, so no change was made")
        chosen = self._one_ref(str(args.get("ref")), "recategorize", {"category": category.value})
        if chosen is None:
            return {"needs_choice": True, "changed": False}
        txn_id = self.refs[chosen]
        txn = self.service.ledger.get(txn_id)
        if txn is None:
            raise ToolError("no such transaction")
        before, after = self.service.recategorize(txn_id, category, self.user_id)
        return self._finish_write(
            {
                "changed": before != after,
                "merchant": plain_merchant(txn.merchant) if txn else None,
                "amount": usd(txn.amount) if txn else None,
                "date": txn.spend_date.isoformat() if txn else None,
                "from": label(before) if before else "not counted",
                "to": label(after),
                "reversible": "Ask to undo the last correction to put it back.",
            }
        )

    def _undo_last_recategorization(self, args: dict[str, Any]) -> dict[str, Any]:
        self._allow_change()
        hints = {
            "budget": "that sounds like a budget change, so use undo_last_budget_change",
            "rule": "that sounds like a merchant rule, so use undo_last_rule_change",
            "adjustment": "that sounds like a charge adjustment, so use "
            "undo_last_charge_adjustment",
            "split": "that sounds like a receipt split, so use undo_last_split",
        }
        if self.undo_kind in hints:
            raise ToolError(hints[self.undo_kind])
        if (earlier := self._already_done()) is not None:
            self.changes_made += 1
            return earlier
        txn_id = self.service.last_correction()
        if txn_id is None:
            raise ToolError("there is no recent correction to undo")
        txn = self.service.ledger.get(txn_id)
        reverted = self.service.undo_recategorization(txn_id, self.user_id)
        return self._finish_write(
            {
                "undone": True,
                "merchant": plain_merchant(txn.merchant) if txn else None,
                "amount": usd(txn.amount) if txn else None,
                "now_in": label(reverted) if reverted else "its original category",
            }
        )

    @staticmethod
    def _budget_data(result: BudgetChange, standing: bool = False) -> dict[str, Any]:
        return {
            **({"standing": True} if standing else {}),
            "month": f"{result.year}-{result.month:02d}",
            "changes": [
                {"category": label(move.category), "was": usd(move.before), "now": usd(move.after)}
                for move in result.moves
            ],
            "budget_was": usd(result.total_before),
            "budget_now": usd(result.total_after),
        }

    def _target_month(self, args: dict[str, Any]) -> tuple[int, int]:
        """The month the person's words point at. A `month` the model passes must agree."""
        target = requested_month(self.question, self.service.today())
        given = self._month(args.get("month"))
        if given != (None, None) and given != target:
            raise ToolError("that is not the month the user asked about")
        return target

    @staticmethod
    def _check_move(move: re.Match[str], category: Category, amount: Decimal) -> None:
        """In "from A to B", A's limit goes down and B's goes up, whatever the model sent."""
        source = mentions_category(move["source"], category)
        target = mentions_category(move["target"], category)
        if source and not target and amount > 0:
            raise ToolError(f"money moves out of {label(category)}, so its change must be negative")
        if target and not source and amount < 0:
            raise ToolError(f"money moves into {label(category)}, so its change must be positive")

    def _change_monthly_budget(self, args: dict[str, Any]) -> dict[str, Any]:
        self._allow_budget_change()
        if (earlier := self._already_done()) is not None:
            self.changes_made += 1
            return earlier
        year, month = self._target_month(args)
        standing = args.get("from_now_on", False)
        if not isinstance(standing, bool):
            raise ToolError("from_now_on must be true or false")
        if standing and not STANDING.search(self.question):
            raise ToolError(
                "the user did not say this is permanent (from now on, every month, always), "
                "so change that one month only"
            )
        raw = args.get("changes")
        if not isinstance(raw, list) or not 1 <= len(raw) <= MAX_LIMIT_CHANGES:
            raise ToolError(f"changes must list 1 to {MAX_LIMIT_CHANGES} categories")
        # The model says which limits and by how much, but it never gets to choose: every
        # category, amount, month and direction must match what the person wrote.
        clause = instruction(self.question)
        # Amounts count across a comma ("this month, to $800"); the category still has to be
        # in the opening clause, which `mentions_category` checks below.
        stated = _stated(_plain(self.question))
        direction = _direction(_plain(self.question))
        move = _MOVE.search(_plain(self.question))
        current = (
            self.service.defaults(year, month) if standing else self.service.limits(year, month)
        )
        changes: list[LimitChange] = []
        for item in raw:
            if not isinstance(item, dict):
                raise ToolError("each change needs a category and an amount")
            category = self._category(item.get("category"))
            if not mentions_category(clause, category):
                raise ToolError(f"the user did not name {label(category)}, so no change was made")
            new_limit, change_by = item.get("new_limit"), item.get("change_by")
            if (new_limit is None) == (change_by is None):
                raise ToolError("give either new_limit or change_by for each category")
            if move is not None and change_by is None:
                raise ToolError("to move money between categories use change_by for each one")
            amount = (
                self._amount(new_limit) if new_limit is not None else self._signed_amount(change_by)
            )
            # First of all, the amount has to be one the person wrote (and not a date's digits).
            words = stated.get(to_cents(abs(amount)))
            if words is None:
                raise ToolError("that amount is not one the user wrote in their request")
            # "$" only records that it was written as money. "to" or "by" decides which field.
            words = words - {"", "$"}
            if new_limit is not None:
                if words == {"by"}:
                    raise ToolError(
                        "the user wrote 'by' that amount, so use change_by, not new_limit"
                    )
                if direction * (amount - current[category]) < 0:
                    raise ToolError(
                        f"that would go the opposite way for {label(category)} from what the "
                        "user asked"
                    )
                changes.append(LimitChange(category, set_to=amount))
            else:
                if move is not None:
                    self._check_move(move, category, amount)
                elif direction * amount < 0:
                    raise ToolError("that goes the opposite way from what the user asked")
                elif words == {"to"}:
                    raise ToolError(
                        "the user wrote 'to' that amount, so use new_limit, not change_by"
                    )
                changes.append(LimitChange(category, change_by=amount))
        if move is not None:
            if len(changes) != 2 or any(change.change_by is None for change in changes):
                raise ToolError(
                    "moving money needs both categories: the one it leaves and the one it joins"
                )
            if changes[0].change_by != -changes[1].change_by:  # type: ignore[operator]
                raise ToolError(
                    "a transfer has to move the same amount out of one category and into the other"
                )
        result = (
            change_defaults(self.service, changes, self.user_id, year, month)
            if standing
            else self.service.change_limits(changes, self.user_id, year, month)
        )
        return self._finish_write(
            {
                "changed": result.changed,
                **self._budget_data(result, standing),
                **({"still_applies": self._still_applies(result)} if standing else {}),
                "reversible": "Ask to undo the last budget change to put it back.",
            }
        )

    def _still_applies(self, result: BudgetChange) -> list[dict[str, str]]:
        """Changed categories whose limit for the month is still a one-off, not the new default."""
        limits = self.service.limits(result.year, result.month)
        defaults = self.service.defaults(result.year, result.month)
        return [
            {"category": label(move.category), "limit": usd(limits[move.category])}
            for move in result.moves
            if move.before != move.after and limits[move.category] != defaults[move.category]
        ]

    def _undo_last_budget_change(self, args: dict[str, Any]) -> dict[str, Any]:
        if not self.budget_undo_allowed:
            raise ToolError("the user did not ask to undo a budget change, so nothing was undone")
        if self.changes_made >= 1:
            self.skipped_writes += 1
            raise ToolError("only one change can be made per request")
        if (earlier := self._already_done()) is not None:
            self.changes_made += 1
            return earlier
        year, month = self._target_month(args)
        standing = newest_budget_change(self.service, year, month) == "default"
        result = (
            undo_defaults(self.service, self.user_id)
            if standing
            else self.service.undo_limit_change(self.user_id, year, month)
        )
        return self._finish_write({"undone": True, **self._budget_data(result, standing)})

    # --- charge adjustments

    def _said(self, term: str) -> bool:
        """Are the words the model searched with (apart from short or generic ones) in the
        person's own sentence, whatever apostrophes and symbols either side used?"""
        said = [" ".join(words) for words in search_words(self.question)]
        for words in search_words(term):
            words = [w for w in words if len(w) >= 3 and w not in GENERIC_WORDS]
            if words and any(all(word in text for word in words) for text in said):
                return True
        return False

    def _one_ref(self, ref: str, action: str, extra: dict[str, Any]) -> str | None:
        """The single charge this request names, or None after listing the candidates."""
        term = self.ref_terms.get(ref, "")
        matches = [
            other for other, known in self.ref_terms.items() if known == term and self._said(known)
        ]
        if len(matches) > 1:
            charges = [self.ref_info[item] for item in matches if item in self.ref_info]
            picked = listed_charge(self.question, charges)
            if picked is not None:
                matches = [
                    item for item in matches if self.ref_info[item]["txn_id"] == picked["txn_id"]
                ]
        if len(matches) == 1:
            return matches[0]
        amount = self.ref_amounts.get(ref)
        if amount is not None and amount in question_amounts(self.question):
            same_amount = [other for other, known in self.ref_amounts.items() if known == amount]
            if len(same_amount) == 1:
                return same_amount[0]
        charges = [self.ref_info[item] for item in matches if item in self.ref_info]
        if len(charges) > 1:
            self.choice_text = choice_reply(charges)
            self.pending_choice = {"action": action, "charges": charges, **extra}
            return None
        raise ToolError("the user did not say which charge, so no change was made")

    def _names_charge(self, ref: str, amount: Decimal) -> bool:
        """Did the person say which charge? The word the model searched to find it (the
        merchant) is in their own sentence, or its card amount is stated and no other charge
        found in this conversation has that amount."""
        term = self.ref_terms.get(ref, "")
        if self._said(term):
            # Two charges found with the same search are ambiguous unless the card amount picks one.
            same = [known for known in self.ref_terms.values() if known == term]
            if len(same) == 1:
                return True
        if amount in question_amounts(self.question):
            return sum(1 for known in self.ref_amounts.values() if known == amount) == 1
        return False

    @staticmethod
    def _adjustment_data(result: AdjustmentResult) -> dict[str, Any]:
        charged = result.charged
        return {
            "merchant": plain_merchant(result.merchant) if result.merchant else None,
            "date": result.spend_date.isoformat() if result.spend_date else None,
            "card_charge": usd(charged) if charged is not None else None,
            "was_counted_as": (
                usd(result.before)
                if result.before is not None
                else (usd(charged) if charged is not None else None)
            ),
            "now_counted_as": (
                usd(result.after)
                if result.after is not None
                else (usd(charged) if charged is not None else None)
            ),
        }

    def _adjust_charge_amount(self, args: dict[str, Any]) -> dict[str, Any]:
        self._allow_write(self.adjustment_allowed, "to adjust what a charge counts for")
        if (earlier := self._already_done()) is not None:
            self.changes_made += 1
            return earlier
        txn_id = self.refs.get(str(args.get("ref")))
        if txn_id is None:
            raise ToolError("ref must come from search_transactions in this conversation")
        txn = self.service.ledger.get(txn_id)
        if txn is None:
            raise ToolError("no such transaction")
        counted, reimbursed = args.get("counted_amount"), args.get("reimbursed_amount")
        if (counted is None) == (reimbursed is None):
            raise ToolError("give either counted_amount or reimbursed_amount")
        amount = self._amount(counted if counted is not None else reimbursed)
        if to_cents(amount) not in question_amounts(self.question):
            raise ToolError("that amount is not one the user wrote in their request")
        chosen = self._one_ref(
            str(args.get("ref")),
            "adjust",
            {
                "counted": str(amount) if counted is not None else "",
                "reimbursed": str(amount) if reimbursed is not None else "",
            },
        )
        if chosen is None:
            return {"needs_choice": True, "changed": False}
        txn_id = self.refs[chosen]
        txn = self.service.ledger.get(txn_id)
        if txn is None:
            raise ToolError("no such transaction")
        asked = _plain(self.question)
        if counted is not None and PAID_BACK.search(asked) and not COUNTS_AS.search(asked):
            raise ToolError("the user described money paid back, so use reimbursed_amount")
        if reimbursed is not None and COUNTS_AS.search(asked) and not PAID_BACK.search(asked):
            raise ToolError("the user said what it really counts for, so use counted_amount")
        if reimbursed is not None and to_cents(amount) == txn.amount and not FULL.search(asked):
            raise ToolError(
                "that would make the whole charge count for nothing, and the user did not say "
                "it was paid back in full"
            )
        note = args.get("note", "")
        if not isinstance(note, str):
            raise ToolError("note must be text")
        result = adjust_charge(
            self.service,
            txn_id,
            self.user_id,
            counted=amount if counted is not None else None,
            reimbursed=amount if reimbursed is not None else None,
            note=note,
        )
        return self._finish_write(
            {
                "changed": result.changed,
                **self._adjustment_data(result),
                "reversible": "Ask to undo the last adjustment to put it back.",
            }
        )

    def _undo_last_charge_adjustment(self, args: dict[str, Any]) -> dict[str, Any]:
        self._allow_write(self.undo_kind == "adjustment", "to undo a charge adjustment")
        if (earlier := self._already_done()) is not None:
            self.changes_made += 1
            return earlier
        result = undo_adjustment(self.service, self.user_id)
        return self._finish_write({"undone": True, **self._adjustment_data(result)})

    def _split_charge(self, args: dict[str, Any]) -> dict[str, Any]:
        self._allow_write(self.split_allowed, "to split a charge across categories")
        if (earlier := self._already_done()) is not None:
            self.changes_made += 1
            return earlier
        if self.refs.get(str(args.get("ref"))) is None:
            raise ToolError("ref must come from search_transactions in this conversation")
        raw = args.get("parts")
        if not isinstance(raw, list):
            raise ToolError("parts must list two categories")
        parts: list[tuple[Category, Decimal]] = []
        for item in raw:
            if not isinstance(item, dict):
                raise ToolError("each part needs a category and an amount")
            category = self._category(item.get("category"))
            if not mentions_category(self.question, category):
                raise ToolError(f"the user did not name {label(category)}, so no change was made")
            amount = self._amount(item.get("amount"))
            if to_cents(amount) not in question_amounts(self.question):
                raise ToolError("that amount is not one the user wrote in their request")
            parts.append((category, amount))
        chosen = self._one_ref(
            str(args.get("ref")),
            "split",
            {
                "parts": [
                    {"category": category.value, "amount": str(amount)}
                    for category, amount in parts
                ]
            },
        )
        if chosen is None:
            return {"needs_choice": True, "changed": False}
        result = split_charge(self.service, self.refs[chosen], self.user_id, parts)
        return self._finish_write(
            {
                "changed": True,
                "merchant": plain_merchant(result.merchant) if result.merchant else None,
                "split": [
                    {"category": label(category), "amount": usd(amount)}
                    for category, amount in result.parts
                ],
                "reversible": "Ask to undo the last split to put it back.",
            }
        )

    def _undo_last_split(self, args: dict[str, Any]) -> dict[str, Any]:
        asked = self.question.lower()
        self._allow_write("undo" in asked and "split" in asked, "to undo a split")
        if (earlier := self._already_done()) is not None:
            self.changes_made += 1
            return earlier
        result = undo_split(self.service, self.user_id)
        return self._finish_write(
            {
                "undone": True,
                "merchant": plain_merchant(result.merchant) if result.merchant else None,
            }
        )

    # --- merchant rules

    def _rule_match(self, merchant: Any) -> str:
        if not isinstance(merchant, str):
            raise ToolError("merchant must be text")
        match = clean_match(_plain(merchant))
        if match not in normalize_match(_plain(self.question), 600):
            raise ToolError("that merchant text is not in what the user wrote, so no rule was made")
        return match

    @staticmethod
    def _rule_data(result: RuleResult) -> dict[str, Any]:
        return {
            "rule": result.match,
            "rule_before": label(result.before) if result.before else None,
            "rule_after": label(result.after) if result.after else None,
            "this_month_charges": result.affected_count,
            "this_month_total": usd(result.affected_total),
        }

    def _set_merchant_rule(self, args: dict[str, Any]) -> dict[str, Any]:
        self._allow_write(self.rule_allowed, "for a standing merchant rule")
        if (earlier := self._already_done()) is not None:
            self.changes_made += 1
            return earlier
        category = self._category(args.get("category"))
        if not mentions_category(self.question, category):
            raise ToolError(f"the user did not name {label(category)}, so no rule was made")
        match = self._rule_match(args.get("merchant"))
        result = set_rule(self.service, match, category, self.user_id)
        return self._finish_write(
            {
                "changed": result.changed,
                **self._rule_data(result),
                "reversible": "Ask to undo the last rule change to put it back.",
            }
        )

    def _remove_merchant_rule(self, args: dict[str, Any]) -> dict[str, Any]:
        self._allow_write(self.rule_removal_allowed, "to remove a merchant rule")
        if (earlier := self._already_done()) is not None:
            self.changes_made += 1
            return earlier
        match = self._rule_match(args.get("merchant"))
        result = remove_rule(self.service, match, self.user_id)
        return self._finish_write(
            {
                "changed": result.changed,
                **self._rule_data(result),
                "reversible": "Ask to undo the last rule change to put it back.",
            }
        )

    def _undo_last_rule_change(self, args: dict[str, Any]) -> dict[str, Any]:
        self._allow_write(self.undo_kind == "rule", "to undo a merchant rule change")
        if (earlier := self._already_done()) is not None:
            self.changes_made += 1
            return earlier
        result = undo_rule_change(self.service, self.user_id)
        return self._finish_write({"undone": True, **self._rule_data(result)})
