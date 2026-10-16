"""Plain-text rendering of budget results. Every figure comes from budget.py, never a model."""

from datetime import date, datetime
from decimal import Decimal

from budget_bot.accounts import account_label
from budget_bot.budget import CategoryDetail, CategoryLine, Coverage, DaySummary, MonthSummary
from budget_bot.categories import INFO, Category, label
from budget_bot.money import percent, usd

NOT_CONNECTED = "Card data isn't connected yet, so there are no spending totals to report."
TOTALS_PREFIX = "Month to date"
HEADLINE_CATEGORIES = (Category.SHOPPING, Category.GROCERIES, Category.DINING)
NEARING = Decimal("0.8")
# Short names for the morning scan. `/ask` still uses the full labels.
SHORT_LABEL = {
    Category.SHOPPING: "Shopping",
    Category.GROCERIES: "Groceries",
    Category.DINING: "Dining",
    Category.HEALTH: "Health",
    Category.HOUSING: "Housing",
    Category.INSURANCE: "Insurance",
    Category.ENTERTAINMENT: "Entertainment",
    Category.PETS: "Pets",
    Category.TRANSPORTATION: "Transport",
    Category.TRAVEL: "Travel",
    Category.BILLS: "Bills",
    Category.SERVICES: "Services",
    Category.MISC: "Misc",
    Category.GIFTS: "Gifts",
}


def when(moment: datetime) -> str:
    hour = moment.hour % 12 or 12
    return f"{moment:%b} {moment.day}, {hour}:{moment:%M %p} CT"


def report_heading(today: date) -> str:
    return f"**{today:%a} {today:%b} {today.day}**"


def days_left_text(days: int) -> str:
    return "1 day left" if days == 1 else f"{days} days left"


def short_label(category: Category) -> str:
    return SHORT_LABEL[category]


def pct_text(part: Decimal, whole: Decimal) -> str:
    value = percent(part, whole)
    if value is None:
        return "n/a"
    # Rounding must not claim a limit is used up while there is still room.
    return f"{min(value, 99) if part < whole else value}%"


def category_line(line: CategoryLine) -> str:
    text = (
        f"{label(line.category)}: {usd(line.committed)} of {usd(line.cap)} "
        f"({pct_text(line.committed, line.cap)})"
    )
    return text + (f", {usd(line.committed - line.cap)} over" if line.over else "")


def pace_text(summary: MonthSummary) -> str:
    used = pct_text(summary.committed, summary.budget)
    elapsed = int(summary.elapsed * 100)
    if summary.pace == "early":
        return "It's early in the month, so pace isn't meaningful yet."
    verdict = {
        "on": "on pace",
        "under": "under pace",
        "over": "ahead of pace (spending faster than the calendar)",
    }[summary.pace]
    projected = (
        f" At this rate the month would end near {usd(summary.projected)}."
        if (summary.projected is not None)
        else ""
    )
    return f"Pace: {used} of the budget used, {elapsed}% of the month elapsed, so {verdict}.{projected}"


def _next_step(summary: MonthSummary) -> str | None:
    """One action, only when the numbers do not already say what to do."""
    if summary.over_budget:
        worst = max(summary.over_budget, key=lambda line: line.committed - line.cap)
        return f"Hold {short_label(worst.category)} the rest of the month."
    if summary.pace == "early":
        return None
    nearing = [
        line
        for line in summary.lines
        if line.cap and line.committed / line.cap >= NEARING and summary.elapsed < Decimal("0.7")
    ]
    if nearing:
        top = max(nearing, key=lambda line: line.committed / line.cap)
        return (
            f"{short_label(top.category)} is at {pct_text(top.committed, top.cap)} "
            f"with {summary.days_left} days left."
        )
    if summary.pace == "over":
        top = max(summary.lines, key=lambda line: line.committed)
        return f"{short_label(top.category)} is the biggest, at {usd(top.committed)}."
    return None


def coverage_text(coverage: Coverage) -> str:
    if not coverage.connected or coverage.newest_success is None:
        return "No card data has been synced yet."
    names = ", ".join(account_label(key) for key in coverage.connected_accounts)
    text = f"Data through {when(coverage.newest_success)} for {names}."
    return text + "".join(f" Heads up: {problem}." for problem in coverage.problems)


def totals_text(summary: MonthSummary) -> str:
    room = (
        f"{usd(summary.remaining)} left"
        if summary.remaining >= 0
        else f"{usd(-summary.remaining)} over budget"
    )
    text = (
        f"{TOTALS_PREFIX}: {usd(summary.committed)} of {usd(summary.budget)} "
        f"({pct_text(summary.committed, summary.budget)}) spent, "
        f"{room}, {summary.days_left} days left including today."
    )
    if summary.pending > 0:
        text += f" That includes {usd(summary.pending)} still pending."
    if summary.refunds:
        text += f" Refunds of {usd(summary.refunds)} are included in that spent total."
    return text


def adjustments_text(summary: MonthSummary) -> str | None:
    """Which limits were changed for this month, and how many charges were counted for less than
    the card shows, so a changed budget or an adjusted total is never hidden. None if neither."""
    parts: list[str] = []
    if summary.limits_changed:
        changed = "; ".join(
            f"{label(line.category)} {usd(line.cap)} "
            f"(usually {usd(line.usual if line.usual is not None else INFO[line.category].cap)})"
            for line in summary.lines
            if line.category in summary.limits_changed
        )
        parts.append(f"Limits changed this month: {changed}.")
    if summary.adjusted_count:
        count = summary.adjusted_count
        parts.append(
            f"{count} charge{'s' if count != 1 else ''} counted for "
            f"{usd(summary.adjusted_total)} less than the card shows."
        )
    return "\n".join(parts) or None


def summary_text(summary: MonthSummary, coverage: Coverage) -> str:
    lines = [totals_text(summary), pace_text(summary)]
    if (adjusted := adjustments_text(summary)) is not None:
        lines.append(adjusted)
    lines += [f"- {category_line(summary.line(category))}" for category in HEADLINE_CATEGORIES]
    over = [line for line in summary.over_budget if line.category not in HEADLINE_CATEGORIES]
    if over:
        lines.append("Also over budget:")
        lines += [f"- {category_line(line)}" for line in over]
    elif not summary.over_budget:
        lines.append("No category is over budget.")
    lines.append(coverage_text(coverage))
    return "\n".join(lines)


def category_text(detail: CategoryDetail, coverage: Coverage) -> str:
    line, summary = detail.line, detail.summary
    verdict = (
        f"{usd(line.remaining)} left" if line.remaining >= 0 else f"{usd(-line.remaining)} over"
    )
    parts = [f"{category_line(line)} — {verdict}, {summary.days_left} days left including today."]
    if line.pending:
        parts.append(f"That includes {usd(line.pending)} pending.")
    parts.append(coverage_text(coverage))
    return " ".join(parts)


def _pace_glance(summary: MonthSummary) -> str:
    if summary.pace == "early" or summary.projected is None:
        return "Too early to judge pace"
    word = {"on": "On pace", "under": "Under pace", "over": "Ahead of pace"}[summary.pace]
    elapsed = int(summary.elapsed * 100)
    return f"**{word}** · {elapsed}% of the month gone · about {usd(summary.projected)}"


def _category_glance(line: CategoryLine) -> str:
    mark = (
        f"**{usd(line.committed - line.cap)} over**"
        if line.over
        else pct_text(line.committed, line.cap)
    )
    return f"{short_label(line.category)} · {usd(line.committed)} / {usd(line.cap)} · {mark}"


def _money_block(summary: MonthSummary) -> str:
    room = (
        f"**{usd(summary.remaining)}** left"
        if summary.remaining >= 0
        else f"**{usd(-summary.remaining)}** over"
    )
    return "\n".join(
        (
            f"**{usd(summary.committed)}** of {usd(summary.budget)} "
            f"({pct_text(summary.committed, summary.budget)})",
            f"{room} · {days_left_text(summary.days_left)}",
            _pace_glance(summary),
        )
    )


def _category_block(summary: MonthSummary) -> str:
    rows = [_category_glance(summary.line(category)) for category in HEADLINE_CATEGORIES]
    extra = [
        _category_glance(line)
        for line in summary.over_budget
        if line.category not in HEADLINE_CATEGORIES
    ]
    if extra:
        rows += ["", *extra]
    return "\n".join(rows)


def _usual_cap(line: CategoryLine) -> Decimal:
    return line.usual if line.usual is not None else INFO[line.category].cap


def _note_lines(summary: MonthSummary, yesterday: DaySummary) -> list[str]:
    notes: list[str] = []
    if summary.pending > 0:
        notes.append(f"Pending {usd(summary.pending)}")
    if summary.refunds:
        notes.append(f"Refunds {usd(summary.refunds)}, already in the total")
    if summary.limits_changed:
        changed = "; ".join(
            f"{short_label(line.category)} {usd(line.cap)} (usually {usd(_usual_cap(line))})"
            for line in summary.lines
            if line.category in summary.limits_changed
        )
        notes.append(f"Limits changed: {changed}.")
    if summary.adjusted_count:
        count = summary.adjusted_count
        notes.append(
            f"{count} charge{'s' if count != 1 else ''} counted "
            f"{usd(summary.adjusted_total)} less than the card shows."
        )
    if (posted := _yesterday_line(yesterday)) is not None:
        notes.append(posted)
    if (step := _next_step(summary)) is not None:
        notes.append(step)
    return notes


def _yesterday_line(day: DaySummary) -> str | None:
    if day.count == 0 or day.largest is None:
        return None
    if day.largest.amount <= 0:
        return f"Yesterday: {usd(-day.total)} refunded."
    name = short_label(day.largest.category)
    if day.count == 1:
        return f"Yesterday: {usd(day.largest.amount)} {name}"
    return (
        f"Yesterday: {usd(day.total)} · {day.count} charges · "
        f"largest {usd(day.largest.amount)} {name}"
    )


def daily_text(
    today: date, summary: MonthSummary, yesterday: DaySummary, coverage: Coverage
) -> str:
    """The 8 AM scan. `/ask summary` is the longer version of the same figures."""
    blocks = [report_heading(today), _money_block(summary), _category_block(summary)]
    if notes := _note_lines(summary, yesterday):
        blocks.append("\n".join(notes))
    blocks.append(coverage_text(coverage))
    return "\n\n".join(blocks)
