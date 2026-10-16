"""Household changes made through chat that are not a month's limits.

- **Charge adjustments**: a charge that counts for less than the card shows (a $200 gift that
  someone paid back $100 of counts for $100).
- **Merchant rules**: charges from a merchant always count in a given category.
- **Standing limits**: a category's default limit from this month (or next) onward.

Each lives on a small versioned setting record that also keeps what undo needs. A change is one
conditional save, so two people changing the same thing at once cannot lose each other's change.
All amounts are exact cents worked out here, never by the model, and every change is audited.
"""

import re
from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, date
from decimal import Decimal
from typing import Any

from budget_bot.budget import (
    BudgetChange,
    BudgetError,
    BudgetService,
    LimitChange,
    LimitMove,
    _month_key,
    _standing_defaults,
    month_bounds,
)
from budget_bot.categories import CATEGORIES, Category
from budget_bot.money import to_cents, usd
from budget_bot.ports import BudgetConflict
from budget_bot.transactions import (
    clean_text,
    normalize_match,
    resolve,
    rules_from,
    specific_enough,
)

SAVE_ATTEMPTS = 3  # tries when someone else changes the same thing at the same moment
MAX_HISTORY = 20  # changes kept for undo
MAX_RULES = 30
MAX_STEPS = 60  # standing limit changes; they are never dropped, so there is a ceiling
MATCH_OK = re.compile(r"[a-z0-9 *&'#.\-]{4,40}")


class ChangeError(BudgetError):
    """A change that is refused; like BudgetError, the message is written to be shown."""


def _stamp(service: BudgetService) -> str:
    return service.clock().astimezone(UTC).isoformat()


# --- charge adjustments -----------------------------------------------------------------------


@dataclass(frozen=True)
class AdjustmentResult:
    txn_id: str
    merchant: str | None
    spend_date: date | None
    charged: Decimal | None  # what the card shows
    before: Decimal | None  # what it counted for before (None: the full amount)
    after: Decimal | None  # what it counts for now (None: the full amount)

    @property
    def changed(self) -> bool:
        return self.before != self.after


def _counted(data: dict[str, Any]) -> Decimal | None:
    raw = data.get("counted")
    try:
        return to_cents(Decimal(str(raw))) if raw else None
    except ValueError, ArithmeticError:
        return None  # a damaged record counts as no adjustment rather than breaking the bot


def adjust_charge(
    service: BudgetService,
    txn_id: str,
    user_id: str,
    *,
    counted: Decimal | None = None,
    reimbursed: Decimal | None = None,
    note: str = "",
) -> AdjustmentResult:
    """Make one purchase count for less than the card shows: either say what it really counts
    for, or how much of it was paid back. The card's own amount is never rewritten."""
    if (counted is None) == (reimbursed is None):
        raise ChangeError("say either what the charge really counts for or how much was paid back")
    txn = service.ledger.get(txn_id)
    if txn is None:
        raise ChangeError("no such transaction")
    if txn.pending:
        raise ChangeError(
            "that charge is still pending and its final amount can change; adjust it once it posts"
        )
    view = next(
        (r for r in service._resolved(txn.spend_date, txn.spend_date) if r.txn.txn_id == txn_id),
        None,
    )
    if view is None or not view.counted:
        raise ChangeError("that charge is not counted in the budget, so there is nothing to adjust")
    if txn.amount <= 0:
        raise ChangeError("only a purchase can be adjusted, not a refund")
    charged = txn.amount
    try:
        amount = to_cents(reimbursed if reimbursed is not None else counted)  # type: ignore[arg-type]
    except ValueError as exc:
        raise ChangeError("that amount is not valid") from exc
    if reimbursed is not None:
        if not 0 < amount <= charged:
            raise ChangeError(
                f"the amount paid back must be more than $0 and no more than the {usd(charged)} "
                "charged"
            )
        target = charged - amount
    else:
        if not 0 <= amount <= charged:
            raise ChangeError(
                f"what the charge counts for must be between $0 and the {usd(charged)} charged"
            )
        target = amount
    after = None if target == charged else target  # counting for the full amount is no adjustment
    note = clean_text(note, 60)
    for _ in range(SAVE_ATTEMPTS):
        record = service.ledger.setting("ADJUST", txn_id)
        before = _counted(record.data)
        if before == after:
            return AdjustmentResult(txn_id, txn.merchant, txn.spend_date, charged, before, after)
        now = _stamp(service)
        entry = {
            "by": user_id,
            "at": now,
            "from": "" if before is None else str(before),
            "to": "" if after is None else str(after),
            "note": note,
        }
        data = {
            "counted": entry["to"],
            "note": note if after is not None else "",
            "by": user_id,
            "at": now,
            "history": [*list(record.data.get("history", [])), entry][-MAX_HISTORY:],
        }
        # Audited under its own subject: a correction's undo history is read from the same trail
        # by transaction ID and must not see this.
        audit = service._audit(user_id, "adjust", f"adjust:{txn_id}", entry["from"], entry["to"])
        try:
            service.ledger.save_setting("ADJUST", txn_id, data, record.revision, audit)
        except BudgetConflict:
            continue
        return AdjustmentResult(txn_id, txn.merchant, txn.spend_date, charged, before, after)
    raise ChangeError("that charge was just changed by someone else; please try again")


def undo_adjustment(service: BudgetService, user_id: str) -> AdjustmentResult:
    """Put back what the most recently adjusted charge counted for before. Repeating walks back."""
    for _ in range(SAVE_ATTEMPTS):
        records = service.ledger.settings_of("ADJUST")
        newest = [
            (str(entry.get("at", "")), key)
            for key, record in records.items()
            for entry in list(record.data.get("history", []))[-1:]
            if isinstance(entry, dict)
        ]
        if not newest:
            raise ChangeError("there is no charge adjustment to undo")
        key = max(newest)[1]
        record = records[key]
        history = list(record.data["history"])
        latest = history.pop()
        restored = str(latest.get("from", ""))
        data = {
            "counted": restored,
            "note": str(history[-1].get("note", "")) if history and restored else "",
            "by": user_id,
            "at": _stamp(service),
            "history": history,
        }
        audit = service._audit(
            user_id,
            "adjust_undo",
            f"adjust:{key}",
            str(latest.get("to", "")),
            str(latest.get("from", "")),
        )
        try:
            service.ledger.save_setting("ADJUST", key, data, record.revision, audit)
        except BudgetConflict:
            continue
        txn = service.ledger.get(key)
        return AdjustmentResult(
            key,
            txn.merchant if txn else None,
            txn.spend_date if txn else None,
            txn.amount if txn else None,
            _counted({"counted": latest.get("to", "")}),
            _counted({"counted": restored}),
        )
    raise ChangeError("that charge was just changed by someone else; please try again")


# --- merchant rules ---------------------------------------------------------------------------


@dataclass(frozen=True)
class RuleResult:
    match: str
    before: Category | None  # the category the rule gave before (None: there was no rule)
    after: Category | None  # the category it gives now (None: no rule)
    affected_count: int  # this month's charges that change category because of it
    affected_total: Decimal

    @property
    def changed(self) -> bool:
        return self.before != self.after


def clean_match(text: str) -> str:
    """The merchant text a rule compares against, or a refusal that explains what is needed."""
    match = normalize_match(text)
    if (
        not MATCH_OK.fullmatch(match)
        or not specific_enough(match)
        or match.replace(" ", "").isdigit()
    ):
        raise ChangeError(
            "a merchant rule needs at least 4 letters or digits (3 are enough with a * as in "
            "WGC*), up to 40 characters that may use * & ' # . -, and a name rather than just "
            "numbers"
        )
    return match


def _rules_in(data: dict[str, Any]) -> list[dict[str, Any]]:
    rules = data.get("rules")
    return (
        [dict(rule) for rule in rules if isinstance(rule, dict)] if isinstance(rules, list) else []
    )


def _category_of(rule: dict[str, Any] | None) -> Category | None:
    try:
        return Category(rule["category"]) if rule else None
    except ValueError, KeyError, TypeError:
        return None


def _effect(service: BudgetService, new_rules: list[dict[str, Any]]) -> tuple[int, Decimal]:
    """How many of this month's counted charges change category under `new_rules`, and their
    total. Worked out by resolving them again, so a more specific rule that keeps a charge where
    it is does not count, and a person's own correction never moves."""
    today = service.today()
    first, last = month_bounds(today.year, today.month)
    rules = rules_from({"rules": new_rules})
    moved = [
        item
        for item in service._resolved(first, last)
        if item.counted
        and item.provenance != "user"
        and resolve(item.txn, None, None, rules).category != item.category
    ]
    return len(moved), sum((item.txn.amount for item in moved), Decimal(0))


def _save_rules(
    service: BudgetService,
    user_id: str,
    action: str,
    match: str,
    mutate: Callable[
        [list[dict[str, Any]]],
        RuleResult | tuple[list[dict[str, Any]], Category | None, Category | None],
    ],
) -> RuleResult:
    for _ in range(SAVE_ATTEMPTS):
        record = service.ledger.setting("RULES", "ALL")
        rules = _rules_in(record.data)
        outcome = mutate(rules)
        if isinstance(outcome, RuleResult):
            return outcome  # nothing to change
        new_rules, before, after = outcome
        count, total = _effect(service, new_rules)
        now = _stamp(service)
        history = [
            *list(record.data.get("history", [])),
            {"rules": rules, "by": user_id, "at": now},
        ]
        data = {"rules": new_rules, "history": history[-MAX_HISTORY:]}
        audit = service._audit(
            user_id,
            action,
            f"rule:{match}",
            before.value if before else "",
            after.value if after else "",
        )
        try:
            service.ledger.save_setting("RULES", "ALL", data, record.revision, audit)
        except BudgetConflict:
            continue
        return RuleResult(match, before, after, count, total)
    raise ChangeError("the merchant rules were just changed by someone else; please try again")


def set_rule(
    service: BudgetService, match_text: str, category: Category, user_id: str
) -> RuleResult:
    """From now on, charges whose merchant text contains `match_text` count in `category`."""
    match = clean_match(match_text)
    now = _stamp(service)

    def mutate(rules: list[dict[str, Any]]) -> Any:
        existing = next((r for r in rules if normalize_match(str(r.get("match"))) == match), None)
        before = _category_of(existing)
        if before == category:
            return RuleResult(match, before, category, 0, Decimal(0))
        if existing is None and len(rules) >= MAX_RULES:
            raise ChangeError(f"there are already {MAX_RULES} merchant rules; remove one first")
        kept = [r for r in rules if r is not existing]
        return (
            [*kept, {"match": match, "category": category.value, "by": user_id, "at": now}],
            before,
            category,
        )

    return _save_rules(service, user_id, "rule_set", match, mutate)


def remove_rule(service: BudgetService, match_text: str, user_id: str) -> RuleResult:
    match = clean_match(match_text)

    def mutate(rules: list[dict[str, Any]]) -> Any:
        existing = next((r for r in rules if normalize_match(str(r.get("match"))) == match), None)
        if existing is None:
            raise ChangeError("there is no merchant rule for that")
        return [r for r in rules if r is not existing], _category_of(existing), None

    return _save_rules(service, user_id, "rule_remove", match, mutate)


def undo_rule_change(service: BudgetService, user_id: str) -> RuleResult:
    """Put the merchant rules back as they were before the latest rule change."""
    for _ in range(SAVE_ATTEMPTS):
        record = service.ledger.setting("RULES", "ALL")
        history = list(record.data.get("history", []))
        if not history:
            raise ChangeError("there is no merchant rule change to undo")
        latest = history.pop()
        restored = [dict(r) for r in latest.get("rules", []) if isinstance(r, dict)]
        was = {normalize_match(str(r.get("match"))): r for r in _rules_in(record.data)}
        will = {normalize_match(str(r.get("match"))): r for r in restored}
        match = next(
            (m for m in [*was, *will] if _category_of(was.get(m)) != _category_of(will.get(m))),
            "",
        )
        before, after = _category_of(was.get(match)), _category_of(will.get(match))
        count, total = _effect(service, restored)
        audit = service._audit(
            user_id,
            "rule_undo",
            f"rule:{match}",
            before.value if before else "",
            after.value if after else "",
        )
        data = {"rules": restored, "history": history}
        try:
            service.ledger.save_setting("RULES", "ALL", data, record.revision, audit)
        except BudgetConflict:
            continue
        return RuleResult(match, before, after, count, total)
    raise ChangeError("the merchant rules were just changed by someone else; please try again")


# --- standing limits --------------------------------------------------------------------------


def change_defaults(
    service: BudgetService,
    changes: list[LimitChange],
    user_id: str,
    year: int | None = None,
    month: int | None = None,
) -> BudgetChange:
    """Change category limits for every month from this one (or next) onward. Earlier months
    keep what they had, and a one-off change to a single month still wins over this.

    The record holds the dated steps (never dropped) and a short list of markers saying when each
    latest step was added. Undo removes the last step, so it needs nothing more."""
    year, month = service._budget_month(year, month)
    key = _month_key(year, month)
    for _ in range(SAVE_ATTEMPTS):
        record = service.ledger.setting("DEFAULTS", "LIMITS")
        current = _standing_defaults(record.data, key)
        proposed, moves = service._plan(current, changes)
        result = BudgetChange(
            year,
            month,
            moves,
            sum(current.values(), Decimal(0)),
            sum(proposed.values(), Decimal(0)),
        )
        if not result.changed:
            return result
        steps = list(record.data.get("steps", []))
        if len(steps) >= MAX_STEPS:
            raise ChangeError("there are too many standing limit changes to add another")
        touched = [move for move in moves if move.before != move.after]
        now = _stamp(service)
        step = {
            "from": key,
            "limits": {move.category.value: str(move.after) for move in touched},
            "by": user_id,
            "at": now,
        }
        history = [*list(record.data.get("history", [])), {"from": key, "at": now}]
        data = {"steps": [*steps, step], "history": history[-MAX_HISTORY:]}
        audit = service._audit(
            user_id,
            "default_change",
            f"default:{key}",
            ",".join(f"{m.category.value}={m.before}" for m in touched),
            ",".join(f"{m.category.value}={m.after}" for m in touched),
        )
        try:
            service.ledger.save_setting("DEFAULTS", "LIMITS", data, record.revision, audit)
        except BudgetConflict:
            continue
        return result
    raise ChangeError("the standing limits were just changed by someone else; please try again")


def undo_defaults(service: BudgetService, user_id: str) -> BudgetChange:
    """Take back the latest standing limit change, for every month it applied to."""
    for _ in range(SAVE_ATTEMPTS):
        record = service.ledger.setting("DEFAULTS", "LIMITS")
        history = list(record.data.get("history", []))
        steps = [dict(s) for s in record.data.get("steps", []) if isinstance(s, dict)]
        if not steps:
            raise ChangeError("there is no standing limit change to undo")
        latest = history.pop() if history else steps[-1]
        key = str(latest.get("from", ""))
        try:
            year, month = int(key[:4]), int(key[5:7])
        except ValueError:
            raise ChangeError("that change cannot be undone automatically") from None
        current = _month_key(*service._budget_month(None, None))
        if key < current:
            raise ChangeError(
                "that standing change started in a past month, so it was left as it is"
            )
        restored = steps[:-1]
        before = _standing_defaults(record.data, key)
        after = _standing_defaults({"steps": restored}, key)
        moves = tuple(
            LimitMove(info.category, before[info.category], after[info.category])
            for info in CATEGORIES
            if before[info.category] != after[info.category]
        )
        audit = service._audit(
            user_id, "default_undo", f"default:{key}", "", ",".join(m.category.value for m in moves)
        )
        data = {"steps": restored, "history": history}
        try:
            service.ledger.save_setting("DEFAULTS", "LIMITS", data, record.revision, audit)
        except BudgetConflict:
            continue
        return BudgetChange(
            year, month, moves, sum(before.values(), Decimal(0)), sum(after.values(), Decimal(0))
        )
    raise ChangeError("the standing limits were just changed by someone else; please try again")


def newest_budget_change(service: BudgetService, year: int, month: int) -> str | None:
    """Which kind of budget change was made most recently for a month: "month" (a one-off
    change to that month) or "default" (a standing change that applies to it), so one undo takes
    back the latest. A standing change that only starts later does not count for this month."""
    key = _month_key(year, month)
    record = service.ledger.budget_record(key)
    month_at = str(record.history[-1].get("at", "")) if record.history else ""
    steps = service.ledger.setting("DEFAULTS", "LIMITS").data.get("history", [])
    last = steps[-1] if steps and isinstance(steps[-1], dict) else {}
    # Only a standing change that starts this month or next is what "undo" should take back.
    # An older one still applies, but undoing it would rewrite past months.
    start = str(last.get("from", ""))
    # A step that starts in a later month does not apply here. A step from before this month
    # still applies, but undoing it would rewrite past months, so it is not what "undo" means.
    default_at = str(last.get("at", "")) if start == key else ""
    if not month_at and not default_at:
        return None
    return "default" if default_at > month_at else "month"


@dataclass(frozen=True)
class SplitResult:
    txn_id: str
    merchant: str | None
    parts: tuple[tuple[Category, Decimal], ...]

    @property
    def changed(self) -> bool:
        return bool(self.parts)


def split_charge(
    service: BudgetService,
    txn_id: str,
    user_id: str,
    parts: list[tuple[Category, Decimal]],
) -> SplitResult:
    """Count one receipt in two categories. The parts must add up to what the charge counts for."""
    txn = service.ledger.get(txn_id)
    if txn is None:
        raise ChangeError("no such transaction")
    if txn.pending:
        raise ChangeError("that charge is still pending; split it once it posts")
    if len(parts) != 2 or len({category for category, _ in parts}) != 2:
        raise ChangeError("a split needs two different categories")
    try:
        amounts = [(category, to_cents(amount)) for category, amount in parts]
    except ValueError as exc:
        raise ChangeError("that amount is not valid") from exc
    if any(amount <= 0 for _, amount in amounts):
        raise ChangeError("each part of a split must be more than $0")
    view = next(
        (
            item
            for item in service._resolved(txn.spend_date, txn.spend_date)
            if item.txn.txn_id == txn_id
        ),
        None,
    )
    counted = view.txn.amount if view and view.counted else None
    if counted is None:
        raise ChangeError("that charge is not counted in the budget, so there is nothing to split")
    if sum((amount for _, amount in amounts), Decimal(0)) != counted:
        raise ChangeError(f"the parts must add up to the {usd(counted)} this charge counts for")
    data = {
        "parts": [
            {"category": category.value, "amount": str(amount)} for category, amount in amounts
        ],
        "by": user_id,
        "at": service.clock().astimezone(UTC).isoformat(),
    }
    record = service.ledger.setting("SPLIT", txn_id)
    audit = service._audit(
        user_id, "split", f"split:{txn_id}", "", ",".join(c.value for c, _ in amounts)
    )
    service.ledger.save_setting("SPLIT", txn_id, data, record.revision, audit)
    return SplitResult(txn_id, txn.merchant, tuple(amounts))


def undo_split(service: BudgetService, user_id: str) -> SplitResult:
    """Remove the most recent receipt split."""
    records = service.ledger.settings_of("SPLIT")
    active = [
        (str(record.data.get("at", "")), key, record)
        for key, record in records.items()
        if record.data.get("parts")
    ]
    if not active:
        raise ChangeError("there is no receipt split to undo")
    _, key, record = max(active)
    audit = service._audit(user_id, "split_undo", f"split:{key}", "split", "")
    service.ledger.save_setting(
        "SPLIT", key, {"parts": [], "by": user_id, "at": ""}, record.revision, audit
    )
    txn = service.ledger.get(key)
    return SplitResult(key, txn.merchant if txn else None, ())
