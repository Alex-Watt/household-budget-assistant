"""Deterministic budget arithmetic over the ledger. The model never computes a total."""

import calendar
import json
from collections import defaultdict
from collections.abc import Callable, Iterable, Mapping, Sequence
from dataclasses import dataclass, replace
from datetime import UTC, date, datetime, timedelta
from decimal import Decimal
from typing import Any
from zoneinfo import ZoneInfo

from budget_bot.accounts import KNOWN_ACCOUNTS, account_label
from budget_bot.categories import CATEGORIES, TOTAL_BUDGET, Category, label
from budget_bot.money import cents, to_cents, usd
from budget_bot.ports import BudgetConflict, FeedStatus, Ledger, SyncState
from budget_bot.transactions import (
    Adjustment,
    AuditEntry,
    HouseholdRule,
    Override,
    Resolved,
    adjustments_from,
    clean_text,
    portions,
    resolve,
    resolve_all,
    rules_from,
    search_matches,
    splits_from,
)

HOUSEHOLD_TIME = ZoneInfo("America/Chicago")
STALE_AFTER = timedelta(hours=36)
# A CSV is imported by hand, so it is allowed to age longer before the report complains.
MANUAL_STALE_AFTER = timedelta(days=10)
# Pace is "on track" while spending stays within five percentage points of the calendar.
PACE_TOLERANCE = Decimal("0.05")
# Too little of the month has passed to say anything useful about pace.
MIN_PACE_ELAPSED = Decimal("0.10")
MAX_SEARCH_DAYS = 400
# What the household may set a category limit to, and the most one month's limits may add up to.
MIN_LIMIT = Decimal("1")
MAX_LIMIT = Decimal("20000")
MAX_TOTAL_BUDGET = Decimal("50000")
MAX_LIMIT_CHANGES = 6
BUDGET_SAVE_ATTEMPTS = 3  # tries when someone else changes the same month at the same moment
MAX_CORRECTION_HISTORY = 20
MAX_BUDGET_HISTORY = 20  # changes kept per month for undo
PERIODS = (
    "today",
    "yesterday",
    "last_7_days",
    "this_week",
    "last_week",
    "this_month",
    "last_month",
    "last_30_days",
)


def month_bounds(year: int, month: int) -> tuple[date, date]:
    return date(year, month, 1), date(year, month, calendar.monthrange(year, month)[1])


def previous_month(year: int, month: int) -> tuple[int, int]:
    return (year - 1, 12) if month == 1 else (year, month - 1)


def resolve_period(name: str, today: date) -> tuple[date, date]:
    """Inclusive dates for a named period. Weeks run Monday to Sunday."""
    monday = today - timedelta(days=today.weekday())
    if name == "today":
        return today, today
    if name == "yesterday":
        return today - timedelta(days=1), today - timedelta(days=1)
    if name == "last_7_days":
        return today - timedelta(days=6), today
    if name == "last_30_days":
        return today - timedelta(days=29), today
    if name == "this_week":
        return monday, today
    if name == "last_week":
        return monday - timedelta(days=7), monday - timedelta(days=1)
    if name == "this_month":
        return today.replace(day=1), today
    if name == "last_month":
        return month_bounds(*previous_month(today.year, today.month))
    raise ValueError("unknown period")


def month_elapsed(now: datetime, year: int, month: int) -> Decimal:
    """Fraction of the calendar month that has passed, exact across daylight saving changes."""
    start = datetime(year, month, 1, tzinfo=HOUSEHOLD_TIME)
    following = date(year + (month == 12), month % 12 + 1, 1)
    end = datetime(following.year, following.month, 1, tzinfo=HOUSEHOLD_TIME)
    # Aware datetimes sharing one tzinfo subtract as wall-clock time, so compare in UTC.
    now_utc, start_utc, end_utc = (value.astimezone(UTC) for value in (now, start, end))
    if now_utc <= start_utc:
        return Decimal(0)
    if now_utc >= end_utc:
        return Decimal(1)
    passed, total = now_utc - start_utc, end_utc - start_utc
    ratio = Decimal(passed.days * 86400 + passed.seconds) / Decimal(
        total.days * 86400 + total.seconds
    )
    return ratio.quantize(Decimal("0.000001"))


@dataclass(frozen=True)
class CategoryLine:
    category: Category
    cap: Decimal
    posted: Decimal
    pending: Decimal
    usual: Decimal | None = None  # the limit the month started from, before a one-off change

    @property
    def committed(self) -> Decimal:
        return self.posted + self.pending

    @property
    def remaining(self) -> Decimal:
        return self.cap - self.committed

    @property
    def over(self) -> bool:
        return self.committed > self.cap


@dataclass(frozen=True)
class MonthSummary:
    year: int
    month: int
    as_of: date
    budget: Decimal
    lines: tuple[CategoryLine, ...]
    days_in_month: int
    days_left: int  # including today; zero for a finished month
    elapsed: Decimal
    excluded_count: int  # payments, transfers, income and duplicates left out of the totals
    limits_changed: tuple[Category, ...] = ()  # categories whose limit differs from the default
    adjusted_count: int = 0  # charges counted for less than the card shows
    adjusted_total: Decimal = Decimal(0)  # how much less, in total
    refunds: Decimal = Decimal(0)  # returns already subtracted from posted and pending

    @property
    def default_budget(self) -> Decimal:
        """The month's budget before any one-off change to its limits."""
        return sum(
            (line.usual if line.usual is not None else line.cap for line in self.lines),
            Decimal(0),
        )

    @property
    def projected_overrun(self) -> Decimal | None:
        """How far past the budget the month would end at the current pace, if it would."""
        projected = self.projected
        return (
            projected - self.budget if projected is not None and projected > self.budget else None
        )

    @property
    def posted(self) -> Decimal:
        return sum((line.posted for line in self.lines), Decimal(0))

    @property
    def pending(self) -> Decimal:
        return sum((line.pending for line in self.lines), Decimal(0))

    @property
    def committed(self) -> Decimal:
        return self.posted + self.pending

    @property
    def remaining(self) -> Decimal:
        return self.budget - self.committed

    @property
    def used(self) -> Decimal:
        return self.committed / self.budget

    @property
    def pace(self) -> str:
        """`early`, `under`, `on` or `over` compared with the share of the month that has passed."""
        if self.elapsed < MIN_PACE_ELAPSED:
            return "early"
        gap = self.used - self.elapsed
        if gap > PACE_TOLERANCE:
            return "over"
        return "under" if gap < -PACE_TOLERANCE else "on"

    @property
    def projected(self) -> Decimal | None:
        """Month-end spending if the pace so far continues; None while it is too early to say."""
        if self.elapsed < MIN_PACE_ELAPSED:
            return None
        return cents(self.committed / self.elapsed)

    def line(self, category: Category) -> CategoryLine:
        return next(line for line in self.lines if line.category == category)

    @property
    def over_budget(self) -> tuple[CategoryLine, ...]:
        return tuple(line for line in self.lines if line.over)


@dataclass(frozen=True)
class MerchantTotal:
    merchant: str
    total: Decimal
    count: int


@dataclass(frozen=True)
class CategoryDetail:
    summary: MonthSummary
    line: CategoryLine
    top_merchants: tuple[MerchantTotal, ...]


@dataclass(frozen=True)
class SearchRow:
    txn_id: str
    spend_date: date
    merchant: str
    amount: Decimal
    category: Category
    pending: bool
    account_key: str
    provenance: str
    charged: Decimal | None = None  # the card's own amount, when the charge was adjusted


@dataclass(frozen=True)
class SearchResult:
    start: date
    end: date
    count: int
    posted: Decimal
    pending: Decimal
    rows: tuple[SearchRow, ...]

    @property
    def total(self) -> Decimal:
        return self.posted + self.pending

    @property
    def truncated(self) -> bool:
        return len(self.rows) < self.count


@dataclass(frozen=True)
class MerchantChange:
    merchant: str
    this_period: Decimal
    last_period: Decimal

    @property
    def delta(self) -> Decimal:
        return self.this_period - self.last_period


@dataclass(frozen=True)
class ChangeExplanation:
    category: Category
    year: int
    month: int
    through_day: int
    this_period: Decimal
    last_period: Decimal
    last_full_month: Decimal
    top_increases: tuple[MerchantChange, ...]

    @property
    def delta(self) -> Decimal:
        return self.this_period - self.last_period


@dataclass(frozen=True)
class PurchaseCheck:
    category: Category
    amount: Decimal
    cap: Decimal
    committed: Decimal
    overall_committed: Decimal
    budget: Decimal = TOTAL_BUDGET  # the month's total, which the household may have changed

    @property
    def category_after(self) -> Decimal:
        return self.cap - self.committed - self.amount

    @property
    def overall_after(self) -> Decimal:
        return self.budget - self.overall_committed - self.amount

    @property
    def fits_category(self) -> bool:
        return self.category_after >= 0

    @property
    def fits_overall(self) -> bool:
        return self.overall_after >= 0


@dataclass(frozen=True)
class DaySummary:
    day: date
    count: int
    total: Decimal
    largest: SearchRow | None


@dataclass(frozen=True)
class FeedCoverage:
    feed: str
    accounts: tuple[str, ...]
    last_success: datetime | None
    last_error: str | None
    stale: bool


@dataclass(frozen=True)
class Coverage:
    feeds: tuple[FeedCoverage, ...]
    connected_accounts: tuple[str, ...]
    missing_accounts: tuple[str, ...]
    newest_success: datetime | None

    @property
    def connected(self) -> bool:
        return any(feed.last_success for feed in self.feeds)

    @property
    def problems(self) -> tuple[str, ...]:
        """Plain-language data problems, judged per card.

        A card is fine if any feed covering it is fresh and error-free, so an old backfill CSV
        or a connection that was replaced does not raise a warning while the card is healthy.
        """
        found: list[str] = []
        covered: list[str] = []
        for feed in self.feeds:
            covered += [key for key in feed.accounts if key not in covered]
            if not feed.accounts and feed.last_error:
                found.append(f"a card connection had a sync error ({feed.last_error})")
        for key in covered:
            feeds = [feed for feed in self.feeds if key in feed.accounts]
            if any(f.last_success and not f.stale and not f.last_error for f in feeds):
                continue
            name = account_label(key)
            errors = [f.last_error for f in feeds if f.last_error]
            if errors and errors[0] == "ITEM_LOGIN_REQUIRED":
                found.append(f"{name} needs to be re-linked")
            elif errors:
                found.append(f"{name} had a sync error ({errors[0]})")
            else:
                successes = [f.last_success for f in feeds if f.last_success]
                if successes:
                    newest = max(successes)
                    found.append(f"{name} has not refreshed since {newest:%b %d}")
                else:
                    found.append(f"{name} has never refreshed")
        if self.connected and self.missing_accounts:
            names = " and ".join(account_label(key) for key in self.missing_accounts)
            found.append(f"{names} is not connected")
        return tuple(found)


class RecategorizeError(ValueError):
    pass


class BudgetError(ValueError):
    """A budget change that is refused; the message is safe to show."""


@dataclass(frozen=True)
class LimitChange:
    """One requested change: set a category's limit to an amount, or move it by an amount."""

    category: Category
    set_to: Decimal | None = None
    change_by: Decimal | None = None


@dataclass(frozen=True)
class LimitMove:
    category: Category
    before: Decimal
    after: Decimal


@dataclass(frozen=True)
class BudgetChange:
    year: int
    month: int
    moves: tuple[LimitMove, ...]
    total_before: Decimal
    total_after: Decimal

    @property
    def changed(self) -> bool:
        return any(move.before != move.after for move in self.moves)


def _month_key(year: int, month: int) -> str:
    return f"{year:04d}-{month:02d}"


def _standing_defaults(data: Mapping[str, Any], key: str) -> dict[Category, Decimal]:
    """Each category's default limit for a month: the built-in one, then every standing change
    that took effect on or before that month, oldest first. Unreadable steps are skipped."""
    limits = {info.category: info.cap for info in CATEGORIES}
    steps = data.get("steps")
    for step in steps if isinstance(steps, list) else []:
        try:
            if str(step["from"]) > key:
                continue
            for name, dollars in step["limits"].items():
                amount = to_cents(Decimal(dollars))
                if MIN_LIMIT <= amount <= MAX_LIMIT:
                    limits[Category(name)] = amount
        except ValueError, ArithmeticError, KeyError, TypeError, AttributeError:
            continue
    return limits


def _limits_json(limits: Mapping[Category, Decimal]) -> str:
    return json.dumps({category.value: str(amount) for category, amount in limits.items()})


def _merchant_totals(items: Iterable[Resolved], category: Category) -> dict[str, MerchantTotal]:
    """Net amount and count per merchant (case-insensitive) for counted items in one category."""
    totals: dict[str, MerchantTotal] = {}
    for item in items:
        for part_category, amount in portions(item):
            if part_category != category:
                continue
            key = item.txn.merchant.lower()
            current = totals.get(key) or MerchantTotal(item.txn.merchant, Decimal(0), 0)
            totals[key] = MerchantTotal(current.merchant, current.total + amount, current.count + 1)
    return totals


def _row(item: Resolved, category: Category) -> SearchRow:
    txn = item.txn
    return SearchRow(
        txn.txn_id,
        txn.spend_date,
        txn.merchant,
        txn.amount,
        category,
        txn.pending,
        txn.account_key,
        item.provenance,
        item.charged,
    )


def _summarize(
    resolved: list[Resolved],
    year: int,
    month: int,
    now: datetime,
    limits: Mapping[Category, Decimal],
    usual: Mapping[Category, Decimal],
) -> MonthSummary:
    first, last = month_bounds(year, month)
    posted: dict[Category, Decimal] = defaultdict(Decimal)
    pending: dict[Category, Decimal] = defaultdict(Decimal)
    excluded = 0
    adjusted_count, adjusted_total = 0, Decimal(0)
    refunds = Decimal(0)
    for item in resolved:
        if not first <= item.txn.spend_date <= last:
            continue
        if item.excluded or item.category is None:
            excluded += 1
            continue
        bucket = pending if item.txn.pending else posted
        for part_category, amount in portions(item):
            bucket[part_category] += amount
            if amount < 0:
                refunds -= amount
        if item.charged is not None:
            adjusted_count += 1
            adjusted_total += item.charged - item.txn.amount
    today = now.astimezone(HOUSEHOLD_TIME).date()
    days = calendar.monthrange(year, month)[1]
    if today > last:
        left = 0
    elif today < first:
        left = days
    else:
        left = days - today.day + 1
    return MonthSummary(
        year=year,
        month=month,
        as_of=min(max(today, first), last),
        budget=sum(limits.values(), Decimal(0)),
        lines=tuple(
            CategoryLine(
                info.category,
                limits[info.category],
                posted[info.category],
                pending[info.category],
                usual[info.category],
            )
            for info in CATEGORIES
        ),
        days_in_month=days,
        days_left=left,
        elapsed=month_elapsed(now, year, month),
        excluded_count=excluded,
        limits_changed=tuple(
            info.category for info in CATEGORIES if limits[info.category] != usual[info.category]
        ),
        adjusted_count=adjusted_count,
        adjusted_total=adjusted_total,
        refunds=refunds,
    )


class BudgetService:
    def __init__(self, ledger: Ledger, state: SyncState, clock: Callable[[], datetime]) -> None:
        self.ledger = ledger
        self.state = state
        self.clock = clock

    def now(self) -> datetime:
        return self.clock().astimezone(HOUSEHOLD_TIME)

    def today(self) -> date:
        return self.now().date()

    def _adjustments(self) -> dict[str, Adjustment]:
        stored = self.ledger.settings_of("ADJUST")
        return adjustments_from({txn_id: record.data for txn_id, record in stored.items()})

    def _rules(self) -> tuple[HouseholdRule, ...]:
        return rules_from(self.ledger.setting("RULES", "ALL").data)

    def _splits(self) -> dict[str, tuple[tuple[Category, Decimal], ...]]:
        stored = self.ledger.settings_of("SPLIT")
        return splits_from({txn_id: record.data for txn_id, record in stored.items()})

    def _resolved(self, start: date, end: date) -> list[Resolved]:
        """Resolve a window, reading a few extra days so pending/posted pairs are not split."""
        loaded = self.ledger.between(start - timedelta(days=10), end + timedelta(days=10))
        resolved = resolve_all(
            loaded, self.ledger.overrides(), self._adjustments(), self._rules(), self._splits()
        )
        return [item for item in resolved if start <= item.txn.spend_date <= end]

    def _month(self, year: int | None, month: int | None) -> tuple[int, int]:
        today = self.today()
        # `or` would read year 0 or month 0 as "not given" and quietly use today's.
        year = today.year if year is None else year
        month = today.month if month is None else month
        if not (2000 <= year <= 2100 and 1 <= month <= 12):
            raise ValueError("month out of range")
        return year, month

    def month_summary(self, year: int | None = None, month: int | None = None) -> MonthSummary:
        year, month = self._month(year, month)
        first, last = month_bounds(year, month)
        return _summarize(
            self._resolved(first, last),
            year,
            month,
            self.now(),
            self.limits(year, month),
            self.defaults(year, month),
        )

    def limits(self, year: int, month: int) -> dict[Category, Decimal]:
        """Every category's limit for a month: its default plus any changes made to that month."""
        stored = self.ledger.budget_record(_month_key(year, month)).limits
        return self._limits_from(stored, self.defaults(year, month))

    def defaults(self, year: int, month: int) -> dict[Category, Decimal]:
        """The limits a month starts from: the built-in defaults and any standing change."""
        data = self.ledger.setting("DEFAULTS", "LIMITS").data
        return _standing_defaults(data, _month_key(year, month))

    @staticmethod
    def _limits_from(
        stored: Mapping[str, str], base: Mapping[Category, Decimal]
    ) -> dict[Category, Decimal]:
        limits = dict(base)
        for key, dollars in stored.items():
            try:
                amount = to_cents(Decimal(dollars))
                category = Category(key)
            except ValueError, ArithmeticError:
                continue  # one unreadable stored value must not break every report
            if MIN_LIMIT <= amount <= MAX_LIMIT:
                limits[category] = amount
        return limits

    def budget_total(self, year: int | None = None, month: int | None = None) -> Decimal:
        year, month = self._month(year, month)
        return sum(self.limits(year, month).values(), Decimal(0))

    def category_detail(
        self, category: Category, year: int | None = None, month: int | None = None
    ) -> CategoryDetail:
        year, month = self._month(year, month)
        first, last = month_bounds(year, month)
        resolved = self._resolved(first, last)
        merchants = sorted(
            _merchant_totals(resolved, category).values(), key=lambda m: (-m.total, m.merchant)
        )
        summary = _summarize(
            resolved, year, month, self.now(), self.limits(year, month), self.defaults(year, month)
        )
        return CategoryDetail(summary, summary.line(category), tuple(merchants[:5]))

    def search(
        self,
        start: date,
        end: date,
        merchant: str | None = None,
        category: Category | None = None,
        limit: int = 10,
    ) -> SearchResult:
        if end < start or (end - start).days > MAX_SEARCH_DAYS:
            raise ValueError("date range must be ordered and under about a year")
        needle = clean_text(merchant, 60).lower() if merchant else ""
        if merchant and len(needle) < 2:
            raise ValueError("merchant text is too short")
        rows: list[SearchRow] = []
        posted = pending = Decimal(0)
        shown: Category | None
        for item in self._resolved(start, end):
            if not item.counted:
                continue
            slices = portions(item)
            if category is not None:
                amount = sum(
                    (part for part_category, part in slices if part_category == category),
                    Decimal(0),
                )
                if amount == 0:
                    continue
                shown = category
            else:
                amount = item.txn.amount
                shown = item.category
            if needle and not search_matches(needle, item.txn.merchant):
                continue
            if shown is None:
                continue
            if item.txn.pending:
                pending += amount
            else:
                posted += amount
            row = _row(item, shown)
            if category is not None and item.parts:
                row = replace(row, amount=amount, category=shown)
            rows.append(row)
        rows.sort(key=lambda row: (row.spend_date, row.amount), reverse=True)
        return SearchResult(start, end, len(rows), posted, pending, tuple(rows[: max(1, limit)]))

    def explain_change(
        self, category: Category, year: int | None = None, month: int | None = None
    ) -> ChangeExplanation:
        year, month = self._month(year, month)
        last_year, last_month = previous_month(year, month)
        first, last = month_bounds(year, month)
        prior_first, prior_last = month_bounds(last_year, last_month)
        today = self.today()
        through = last.day if today > last else min(today.day, last.day)
        this = _merchant_totals(
            self._resolved(first, first + timedelta(days=through - 1)), category
        )
        prior = self._resolved(prior_first, prior_last)
        same_days = [item for item in prior if item.txn.spend_date.day <= through]
        before = _merchant_totals(same_days, category)
        full_last = sum((m.total for m in _merchant_totals(prior, category).values()), Decimal(0))
        changes = [
            MerchantChange(
                (this.get(key) or before[key]).merchant,
                this[key].total if key in this else Decimal(0),
                before[key].total if key in before else Decimal(0),
            )
            for key in sorted(this.keys() | before.keys())
        ]
        increases = sorted(
            (c for c in changes if c.delta > 0), key=lambda c: (-c.delta, c.merchant)
        )
        return ChangeExplanation(
            category,
            year,
            month,
            through,
            sum((c.this_period for c in changes), Decimal(0)),
            sum((c.last_period for c in changes), Decimal(0)),
            full_last,
            tuple(increases[:5]),
        )

    def check_purchase(self, category: Category, amount: Decimal) -> PurchaseCheck:
        amount = to_cents(amount)
        if amount <= 0:
            raise ValueError("amount must be positive")
        summary = self.month_summary()
        line = summary.line(category)
        return PurchaseCheck(
            category, amount, line.cap, line.committed, summary.committed, summary.budget
        )

    def posted_on(self, day: date) -> DaySummary:
        """Counted purchases and refunds that posted on one date, newest and largest first."""
        rows = sorted(
            (
                _row(item, item.category)
                for item in self._resolved(day - timedelta(days=45), day)
                if item.category is not None
                and item.counted
                and not item.txn.pending
                and (item.txn.posted_date or item.txn.spend_date) == day
            ),
            key=lambda row: -row.amount,
        )
        return DaySummary(
            day, len(rows), sum((r.amount for r in rows), Decimal(0)), rows[0] if rows else None
        )

    def repeating_charges(self) -> tuple[tuple[str, Decimal, int], ...]:
        """Merchants with a similar amount in at least two of the last four months.

        Each result is the merchant text, the typical monthly amount, and how many months.
        """
        today = self.today()
        year, month = today.year, today.month
        for _ in range(3):
            month -= 1
            if month == 0:
                month, year = 12, year - 1
        groups: dict[str, tuple[str, dict[str, Decimal]]] = {}
        for item in self._resolved(date(year, month, 1), today):
            if not item.counted or item.txn.pending or item.txn.amount <= 0:
                continue
            key = " ".join(item.txn.merchant.lower().split())
            name, months = groups.get(key, (item.txn.merchant, {}))
            stamp = f"{item.txn.spend_date:%Y-%m}"
            months[stamp] = months.get(stamp, Decimal(0)) + item.txn.amount
            groups[key] = (name, months)
        found: list[tuple[str, Decimal, int]] = []
        for name, months in groups.values():
            if len(months) < 2:
                continue
            amounts = sorted(months.values())
            typical = amounts[len(amounts) // 2]
            room = max(Decimal("1.00"), (typical * Decimal("0.10")).quantize(Decimal("0.01")))
            if any(abs(amount - typical) > room for amount in amounts):
                continue
            found.append((name, typical, len(months)))
        found.sort(key=lambda row: (-row[2], -row[1], row[0].lower()))
        return tuple(found[:8])

    def coverage(self) -> Coverage:
        now = self.clock().astimezone(UTC)
        feeds = tuple(self._feed_coverage(status, now) for status in self.state.statuses())
        connected = sorted({key for feed in feeds if feed.last_success for key in feed.accounts})
        missing = tuple(key for key in KNOWN_ACCOUNTS if key not in connected)
        newest = max((feed.last_success for feed in feeds if feed.last_success), default=None)
        return Coverage(feeds, tuple(connected), missing, newest)

    @staticmethod
    def _feed_coverage(status: FeedStatus, now: datetime) -> FeedCoverage:
        success = datetime.fromisoformat(status.last_success) if status.last_success else None
        return FeedCoverage(
            status.feed,
            status.accounts,
            success.astimezone(HOUSEHOLD_TIME) if success else None,
            status.last_error,
            success is None
            or now - success
            > (MANUAL_STALE_AFTER if status.feed.startswith("csv:") else STALE_AFTER),
        )

    def _audit(self, user_id: str, action: str, txn_id: str, old: str, new: str) -> AuditEntry:
        return AuditEntry(
            self.clock().astimezone(UTC).isoformat(), user_id, action, txn_id, old, new
        )

    def _budget_month(self, year: int | None, month: int | None) -> tuple[int, int]:
        """Only this month's and next month's budgets can change; earlier months stay as they were."""
        today = self.today()
        year, month = self._month(year, month)
        following = (today.year + (today.month == 12), today.month % 12 + 1)
        if (year, month) not in {(today.year, today.month), following}:
            raise BudgetError("only this month's or next month's budget can be changed")
        return year, month

    @staticmethod
    def _merged(
        stored: dict[str, str],
        limits: Iterable[tuple[Category, Decimal]],
        defaults: Mapping[Category, Decimal],
    ) -> dict[str, str]:
        """Stored changes with these limits applied. A limit equal to its default is not stored."""
        merged = dict(stored)
        for category, amount in limits:
            if amount == defaults[category]:
                merged.pop(category.value, None)
            else:
                merged[category.value] = str(amount)
        return merged

    def _plan(
        self, current: Mapping[Category, Decimal], changes: Sequence[LimitChange]
    ) -> tuple[dict[Category, Decimal], tuple[LimitMove, ...]]:
        """Check every requested change against a month's current limits: all or nothing."""
        if not 1 <= len(changes) <= MAX_LIMIT_CHANGES:
            raise BudgetError(f"change between 1 and {MAX_LIMIT_CHANGES} categories at a time")
        if len({change.category for change in changes}) != len(changes):
            raise BudgetError("each category can appear only once in a request")
        proposed = dict(current)
        for change in changes:
            if (change.set_to is None) == (change.change_by is None):
                raise BudgetError("give either a new limit or an amount to change by, not both")
            try:
                if change.set_to is not None:
                    target = to_cents(change.set_to)
                else:
                    assert change.change_by is not None
                    target = current[change.category] + to_cents(change.change_by)
            except ValueError as exc:
                raise BudgetError("that amount is not valid") from exc
            if not MIN_LIMIT <= target <= MAX_LIMIT:
                raise BudgetError(
                    f"{label(change.category)} must stay between {usd(MIN_LIMIT)} "
                    f"and {usd(MAX_LIMIT)}"
                )
            proposed[change.category] = target
        if sum(proposed.values(), Decimal(0)) > MAX_TOTAL_BUDGET:
            raise BudgetError(f"the month's budget cannot go above {usd(MAX_TOTAL_BUDGET)}")
        moves = tuple(
            LimitMove(change.category, current[change.category], proposed[change.category])
            for change in changes
        )
        return proposed, moves

    def change_limits(
        self,
        changes: Sequence[LimitChange],
        user_id: str,
        year: int | None = None,
        month: int | None = None,
    ) -> BudgetChange:
        """Change category limits for one month. All arithmetic happens here, in exact cents.

        Either every requested change is applied or none is. The change is recorded on the
        month's budget record, so `undo_limit_change` can put it back, and audited. If someone
        else changes the same month at the same moment, this re-reads and re-checks rather than
        overwriting their change.
        """
        year, month = self._budget_month(year, month)
        key = _month_key(year, month)
        for _ in range(BUDGET_SAVE_ATTEMPTS):
            defaults = self.defaults(year, month)
            record = self.ledger.budget_record(key)
            current = self._limits_from(record.limits, defaults)
            proposed, moves = self._plan(current, changes)
            result = BudgetChange(
                year,
                month,
                moves,
                sum(current.values(), Decimal(0)),
                sum(proposed.values(), Decimal(0)),
            )
            if not result.changed:
                return result
            touched = [move for move in moves if move.before != move.after]
            before = {move.category: move.before for move in touched}
            after = {move.category: move.after for move in touched}
            entry = {
                "by": user_id,
                "at": self.clock().astimezone(UTC).isoformat(),
                "from": {category.value: str(amount) for category, amount in before.items()},
                "to": {category.value: str(amount) for category, amount in after.items()},
            }
            audit = self._audit(
                user_id, "budget_change", f"budget:{key}", _limits_json(before), _limits_json(after)
            )
            try:
                self.ledger.save_budget_record(
                    key,
                    self._merged(record.limits, after.items(), defaults),
                    [*record.history, entry][-MAX_BUDGET_HISTORY:],
                    record.revision,
                    audit,
                )
            except BudgetConflict:
                continue  # changed by someone else a moment ago: look again
            return result
        raise BudgetError("the budget was just changed by someone else; please try again")

    def undo_limit_change(
        self, user_id: str, year: int | None = None, month: int | None = None
    ) -> BudgetChange:
        """Put back the limits the latest change replaced. Repeating walks further back."""
        year, month = self._budget_month(year, month)
        key = _month_key(year, month)
        for _ in range(BUDGET_SAVE_ATTEMPTS):
            defaults = self.defaults(year, month)
            record = self.ledger.budget_record(key)
            if not record.history:
                raise BudgetError("there is no budget change to undo for that month")
            latest = record.history[-1]
            try:
                previous = {
                    Category(name): to_cents(Decimal(amount))
                    for name, amount in latest["from"].items()
                }
            except (ValueError, ArithmeticError, AttributeError, KeyError, TypeError) as exc:
                raise BudgetError("that budget change cannot be undone automatically") from exc
            current = self._limits_from(record.limits, defaults)
            restored = {**current, **previous}
            audit = self._audit(
                user_id,
                "budget_undo",
                f"budget:{key}",
                json.dumps(latest.get("to", {})),
                json.dumps(latest.get("from", {})),
            )
            try:
                self.ledger.save_budget_record(
                    key,
                    self._merged(record.limits, previous.items(), defaults),
                    record.history[:-1],
                    record.revision,
                    audit,
                )
            except BudgetConflict:
                continue
            moves = tuple(LimitMove(c, current[c], amount) for c, amount in previous.items())
            return BudgetChange(
                year,
                month,
                moves,
                sum(current.values(), Decimal(0)),
                sum(restored.values(), Decimal(0)),
            )
        raise BudgetError("the budget was just changed by someone else; please try again")

    def recategorize(
        self, txn_id: str, category: Category, user_id: str
    ) -> tuple[Category | None, Category]:
        """Set a reversible correction. Returns the category before and after."""
        txn = self.ledger.get(txn_id)
        if txn is None:
            raise RecategorizeError("no such transaction")
        # Judge "before" from the same view the household sees, so a correction carried over from
        # a pending twin or a duplicate is respected.
        view = next(
            (r for r in self._resolved(txn.spend_date, txn.spend_date) if r.txn.txn_id == txn_id),
            None,
        )
        before = view.category if view else resolve(txn, None, None, self._rules()).category
        if before == category:
            return before, category
        now = self.clock().astimezone(UTC).isoformat()
        audit = self._audit(
            user_id, "recategorize", txn_id, before.value if before else "excluded", category.value
        )
        for _ in range(BUDGET_SAVE_ATTEMPTS):
            existing = self.ledger.overrides().get(txn_id)
            prior = list(existing.history) if existing and existing.history else []
            if existing and not prior:
                prior = [existing.category.value]
            override = Override(
                txn_id,
                category,
                user_id,
                now,
                tuple([*prior, category.value][-MAX_CORRECTION_HISTORY:]),
                existing.revision if existing else 0,
            )
            try:
                self.ledger.set_override(override, audit)
            except BudgetConflict:
                continue
            return before, category
        raise RecategorizeError("that charge was just changed by someone else; please try again")

    def last_correction(self) -> str | None:
        """The transaction whose correction was made most recently and is still in force."""
        active = self.ledger.overrides()
        if not active:
            return None
        return max(active.values(), key=lambda item: item.set_at).txn_id

    def undo_recategorization(self, txn_id: str, user_id: str) -> Category | None:
        """Undo the latest correction on a transaction and return the category it falls back to.

        With an earlier correction the transaction returns to that one; otherwise to what the
        provider, a pending twin and the household rules say. The history lives on the correction
        itself, so it does not depend on how many other changes are in the audit log.
        """
        for _ in range(BUDGET_SAVE_ATTEMPTS):
            existing = self.ledger.overrides().get(txn_id)
            if existing is None:
                raise RecategorizeError("no correction to undo")
            history = list(existing.history or (existing.category.value,))
            history.pop()
            now = self.clock().astimezone(UTC).isoformat()
            if history:
                previous = Category(history[-1])
                try:
                    self.ledger.set_override(
                        Override(txn_id, previous, user_id, now, tuple(history), existing.revision),
                        self._audit(
                            user_id, "undo", txn_id, existing.category.value, previous.value
                        ),
                    )
                except BudgetConflict:
                    continue
                return previous
            txn = self.ledger.get(txn_id)
            fallback = resolve(txn, None, None, self._rules()).category if txn else None
            self.ledger.clear_override(
                txn_id,
                self._audit(
                    user_id,
                    "undo",
                    txn_id,
                    existing.category.value,
                    fallback.value if fallback else "unknown",
                ),
                existing.revision,
            )
            if txn is None:
                return None
            # The household view also follows a pending twin, which `resolve` on this record misses.
            view = next(
                (
                    item
                    for item in self._resolved(txn.spend_date, txn.spend_date)
                    if item.txn.txn_id == txn_id
                ),
                None,
            )
            return view.category if view else fallback
        raise RecategorizeError("that charge was just changed by someone else; please try again")
