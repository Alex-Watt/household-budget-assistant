"""Normalized transactions, reversible overrides and the rules that decide what counts."""

import re
import unicodedata
from collections import defaultdict
from collections.abc import Callable, Iterable, Mapping, Sequence
from dataclasses import dataclass, replace
from datetime import date
from decimal import Decimal
from typing import Any, Protocol

from budget_bot.accounts import validate_account_key
from budget_bot.categories import (
    CREDIT_ONLY_PRIMARY,
    NON_SPEND_PRIMARY,
    Category,
    apply_merchant_rules,
    map_provider_category,
)
from budget_bot.money import to_cents

MAX_TEXT = 80
MARKUP = frozenset("[]<>`|")
TXN_ID = re.compile(r"[A-Za-z0-9:_.\-]{1,160}\Z")
# A credit that reads like a card payment, whatever category the provider gave it.
PAYMENT_NAME = re.compile(r"\b(autopay|automatic payment|payment thank you|online payment)\b", re.I)
# A credit from a store reduces the month. These names are a payment, paycheck, rewards
# credit, or movement between accounts, so they do not.
_NOT_A_RETURN = re.compile(
    r"\b(?:autopay|automatic payment|payment thank you|online payment|card payment|"
    r"thank you|payroll|paycheck|direct deposit|interest|dividend|"
    r"zelle|venmo|cash app|rewards?|cash ?back|statement credit|"
    r"transfer|deposit|wire|irs|treasury|tax refund|payment)\b",
    re.IGNORECASE,
)
# Amazon purchases are reported as shopping. A return the bank did not categorize still is.
_AMAZON = re.compile(r"\b(?:amazon|amzn)\b", re.IGNORECASE)


def clean_text(value: str, limit: int = MAX_TEXT) -> str:
    """Merchant text is untrusted: drop control/format characters, collapse spaces, bound length.

    Characters that build Discord links, spoilers or code spans are dropped too, so a payee name
    cannot render as a clickable link in a channel message.
    """
    chars = []
    for char in value:
        if char.isspace():
            chars.append(" ")
        elif unicodedata.category(char)[0] != "C" and char not in MARKUP:
            chars.append(char)
    return " ".join("".join(chars).split())[:limit]


@dataclass(frozen=True)
class Transaction:
    txn_id: str
    # Where the record came from: "plaid:<item id>", "csv:<account key>" or "fixture".
    feed: str
    account_key: str
    # Positive is spending; negative is a refund or credit. This is Plaid's sign convention.
    amount: Decimal
    # The day the purchase was authorized (else posted); decides which month it belongs to.
    spend_date: date
    posted_date: date | None
    pending: bool
    merchant: str
    provider_primary: str | None = None
    provider_detailed: str | None = None
    currency: str = "USD"
    # For a posted transaction that replaces a pending one, the pending record's ID.
    pending_of: str | None = None

    def __post_init__(self) -> None:
        if not TXN_ID.fullmatch(self.txn_id) or not self.feed or len(self.feed) > 80:
            raise ValueError("invalid transaction or feed ID")
        validate_account_key(self.account_key)
        if to_cents(self.amount) != self.amount:
            raise ValueError("amount must be whole cents")
        if not self.merchant or self.merchant != clean_text(self.merchant):
            raise ValueError("merchant must be non-empty, cleaned text")
        if not re.fullmatch(r"[A-Z]{3}", self.currency):
            raise ValueError("currency must be an ISO code")
        if self.pending and self.posted_date is not None:
            raise ValueError("a pending transaction has no posted date")

    def to_item(self) -> dict[str, Any]:
        item: dict[str, Any] = {
            "txn_id": self.txn_id,
            "feed": self.feed,
            "account_key": self.account_key,
            "amount": self.amount,
            "spend_date": self.spend_date.isoformat(),
            "pending": self.pending,
            "merchant": self.merchant,
            "currency": self.currency,
        }
        optional = {
            "posted_date": self.posted_date.isoformat() if self.posted_date else None,
            "provider_primary": self.provider_primary,
            "provider_detailed": self.provider_detailed,
            "pending_of": self.pending_of,
        }
        item.update({key: value for key, value in optional.items() if value is not None})
        return item

    @classmethod
    def from_item(cls, item: Mapping[str, Any]) -> Transaction:
        posted = item.get("posted_date")
        return cls(
            txn_id=item["txn_id"],
            feed=item["feed"],
            account_key=item["account_key"],
            amount=Decimal(str(item["amount"])),
            spend_date=date.fromisoformat(item["spend_date"]),
            posted_date=date.fromisoformat(posted) if posted else None,
            pending=bool(item["pending"]),
            merchant=item["merchant"],
            provider_primary=item.get("provider_primary"),
            provider_detailed=item.get("provider_detailed"),
            currency=item.get("currency", "USD"),
            pending_of=item.get("pending_of"),
        )


@dataclass(frozen=True)
class Override:
    """A person's correction. The provider's category is never rewritten, so this is reversible.

    `history` is the categories still in force, oldest first, so undo does not depend on the
    audit log. `revision` is what a save must match, so two undos at once cannot both apply.
    """

    txn_id: str
    category: Category
    set_by: str
    set_at: str
    history: tuple[str, ...] = ()
    revision: int = 0


@dataclass(frozen=True)
class Adjustment:
    """What a charge counts for when that is less than the card shows.

    A gift of $200 that someone paid back half of counts for $100. The card's own amount is
    never rewritten, so this is reversible, and it follows the purchase like a correction does.
    """

    txn_id: str
    counted: Decimal
    note: str
    set_by: str
    set_at: str


@dataclass(frozen=True)
class HouseholdRule:
    """A standing instruction: charges whose merchant text contains `match` count in `category`."""

    match: str  # normalized: lowercase, single spaces
    category: Category
    set_by: str
    set_at: str


def normalize_match(text: str, limit: int = MAX_TEXT) -> str:
    """Merchant text as rules compare it: cleaned, lowercase, single spaces."""
    return " ".join(clean_text(text, limit).lower().split())


def specific_enough(match: str) -> bool:
    """Is rule text specific enough to be safe? 4 or more letters or digits, or 3 plus the `*`
    that card processors put in their prefixes ("wgc*", which only matches that exact text)."""
    letters = len(re.sub(r"[^a-z0-9]", "", match))
    return letters >= 4 or (letters == 3 and "*" in match)


def _readings(text: str) -> tuple[str, str]:
    """Two readings of text without apostrophes, because banks write names either way: the
    possessive s kept ("macys", "ruths chris") and dropped ("macy", "ruth chris")."""
    plain = re.sub("[\u2018\u2019\u02bc\u2032`]", "'", text)
    return plain.replace("'", ""), re.sub(r"'s\b", "", plain).replace("'", "")


def search_words(text: str) -> list[list[str]]:
    """The lowercase words of merchant or search text, once for each reading of its apostrophes.
    Any symbol separates words, so "WGC*RUTH CHRIS" is wgc, ruth, chris."""
    cleaned = clean_text(text, 600).lower()
    return [re.findall(r"[^\W_]+", reading) for reading in _readings(cleaned)]


def search_matches(needle: str, merchant: str) -> bool:
    """Does a search text find a merchant? The text appears in it as typed, or every word of the
    text appears in it, whatever apostrophes and symbols the bank used."""
    if needle.lower() in merchant.lower():
        return True
    found = [" ".join(words) for words in search_words(merchant)]
    for words in search_words(needle):
        words = [word for word in words if len(word) >= 2]
        if words and any(all(word in text for word in words) for text in found):
            return True
    return False


def merchant_matches(match: str, merchant: str) -> bool:
    """Does a rule's text appear in a merchant name as whole words? "mart" must not match
    Walmart. A symbol at either end of the text (the `*` in "wgc*") needs no word boundary.
    Apostrophes are ignored on both sides, in either reading."""
    texts = _readings(normalize_match(merchant))
    for rule in _readings(match):
        if not rule:
            continue
        before = r"(?<![a-z0-9])" if rule[0].isalnum() else ""
        after = r"(?![a-z0-9])" if rule[-1].isalnum() else ""
        pattern = f"{before}{re.escape(rule)}{after}"
        if any(re.search(pattern, text) for text in texts):
            return True
    return False


def rule_for(merchant: str, rules: Sequence[HouseholdRule]) -> HouseholdRule | None:
    """The household rule that applies to a merchant: the most specific, then the newest."""
    found = [rule for rule in rules if merchant_matches(rule.match, merchant)]
    return max(found, key=lambda rule: (len(rule.match), rule.set_at), default=None)


def adjustments_from(stored: Mapping[str, Mapping[str, Any]]) -> dict[str, Adjustment]:
    """Active adjustments from their stored data, by transaction. Unreadable ones are skipped."""
    found: dict[str, Adjustment] = {}
    for txn_id, data in stored.items():
        try:
            if not data.get("counted"):
                continue  # an adjustment that was undone
            counted = to_cents(Decimal(str(data["counted"])))
            if counted < 0:
                continue
            found[txn_id] = Adjustment(
                txn_id,
                counted,
                clean_text(str(data.get("note", "")), 60),
                str(data.get("by", "")),
                str(data.get("at", "")),
            )
        except ValueError, ArithmeticError, TypeError, AttributeError:
            continue
    return found


def splits_from(
    stored: Mapping[str, Mapping[str, Any]],
) -> dict[str, tuple[tuple[Category, Decimal], ...]]:
    """Active receipt splits from their stored data. A split that does not add up is skipped."""
    found: dict[str, tuple[tuple[Category, Decimal], ...]] = {}
    for txn_id, data in stored.items():
        try:
            raw = data.get("parts")
            if not isinstance(raw, list) or len(raw) < 2:
                continue
            parts = tuple(
                (Category(str(part["category"])), to_cents(Decimal(str(part["amount"]))))
                for part in raw
            )
            if any(amount <= 0 for _, amount in parts):
                continue
            found[txn_id] = parts
        except ValueError, ArithmeticError, TypeError, AttributeError, KeyError:
            continue
    return found


def rules_from(data: Mapping[str, Any]) -> tuple[HouseholdRule, ...]:
    """The household's merchant rules from their stored data. Unreadable ones are skipped."""
    found: list[HouseholdRule] = []
    for entry in data.get("rules", []) if isinstance(data.get("rules"), list) else []:
        try:
            match = normalize_match(str(entry["match"]))
            if specific_enough(match):
                found.append(
                    HouseholdRule(
                        match,
                        Category(entry["category"]),
                        str(entry.get("by", "")),
                        str(entry.get("at", "")),
                    )
                )
        except ValueError, KeyError, TypeError, AttributeError:
            continue
    return tuple(found)


@dataclass(frozen=True)
class AuditEntry:
    at: str
    user_id: str
    action: str
    txn_id: str
    from_category: str
    to_category: str


@dataclass(frozen=True)
class Resolved:
    txn: Transaction
    category: Category | None
    provenance: str
    excluded: str | None = None
    charged: Decimal | None = None  # the card's own amount, when an adjustment changed it
    # When a receipt is split, each pair is (category, amount) and the amounts sum to what counts.
    parts: tuple[tuple[Category, Decimal], ...] = ()

    @property
    def counted(self) -> bool:
        return self.excluded is None and self.category is not None


def portions(item: Resolved) -> tuple[tuple[Category, Decimal], ...]:
    """How a counted charge is divided across categories. One category is the whole amount."""
    if item.parts:
        return item.parts
    if item.category is None:
        return ()
    return ((item.category, item.txn.amount),)


def _feed_rank(feed: str) -> tuple[int, str]:
    kind = feed.split(":", 1)[0]
    return ({"plaid": 0, "csv": 1}.get(kind, 2), feed)


# Same card, same amount, in two feeds, with dates this close: one purchase seen twice. A bank
# export and Plaid can disagree by a day about when something happened.
DUPLICATE_DAYS = 1
# A pending record in one feed and the posted record that replaced it in another.
PENDING_LAG_DAYS = 4


def _gap(first: Transaction, second: Transaction) -> int:
    """Fewest days between any date of one record and any date of the other."""
    one = {first.spend_date, first.posted_date or first.spend_date}
    two = {second.spend_date, second.posted_date or second.spend_date}
    return min(abs((a - b).days) for a in one for b in two)


def _by_date(records: Iterable[Transaction]) -> list[Transaction]:
    return sorted(records, key=lambda txn: (txn.spend_date, txn.txn_id))


def find_duplicates(transactions: Iterable[Transaction]) -> dict[str, str]:
    """Map each transaction that must not be counted to the reason.

    - A pending record is dropped once a posted record names it (`replaced_by`).
    - A pending record is also dropped when another feed already has the posted version
      (`superseded_by`), for example a Plaid pending charge that a CSV export shows as posted.
    - The same card imported through several feeds (Plaid and a CSV, or a duplicated Plaid
      Item) counts once (`duplicate_of`). The highest-priority feed wins and records pair off
      one-to-one, so two genuine identical purchases inside one feed both still count.
    """
    by_id = {txn.txn_id: txn for txn in transactions}
    dropped: dict[str, str] = {}
    for txn in by_id.values():
        replaced = by_id.get(txn.pending_of) if txn.pending_of else None
        if replaced is not None and replaced.pending:
            dropped[replaced.txn_id] = f"replaced_by:{txn.txn_id}"
    groups: dict[tuple[str, Decimal], list[Transaction]] = defaultdict(list)
    for txn in by_id.values():
        if txn.txn_id not in dropped:
            groups[(txn.account_key, txn.amount)].append(txn)
    for group in groups.values():
        if len({txn.feed for txn in group}) < 2:
            continue
        posted = [txn for txn in group if not txn.pending]
        claimed: set[tuple[str, str]] = set()  # (posted ID, pending feed) already used once
        for pending in _by_date(txn for txn in group if txn.pending):
            twin = next(
                (
                    q
                    for q in _by_date(posted)
                    if q.feed != pending.feed
                    and (q.txn_id, pending.feed) not in claimed
                    and _gap(pending, q) <= PENDING_LAG_DAYS
                ),
                None,
            )
            if twin is not None:
                dropped[pending.txn_id] = f"superseded_by:{twin.txn_id}"
                claimed.add((twin.txn_id, pending.feed))
        for is_pending in (False, True):
            records = [t for t in group if t.pending is is_pending and t.txn_id not in dropped]
            kept: list[Transaction] = []
            for feed in sorted({t.feed for t in records}, key=_feed_rank):
                used: set[str] = set()
                survivors: list[Transaction] = []
                for txn in _by_date(t for t in records if t.feed == feed):
                    twin = next(
                        (
                            k
                            for k in kept
                            if k.txn_id not in used and _gap(txn, k) <= DUPLICATE_DAYS
                        ),
                        None,
                    )
                    if twin is None:
                        survivors.append(txn)
                    else:
                        dropped[txn.txn_id] = f"duplicate_of:{twin.txn_id}"
                        used.add(twin.txn_id)
                kept.extend(survivors)
    return dropped


def _merchant_return(txn: Transaction) -> bool:
    """A credit from a store. Card payments, paychecks, rewards, and account transfers are not."""
    return (
        txn.amount < 0
        and txn.merchant != "Unknown merchant"
        and _NOT_A_RETURN.search(txn.merchant) is None
    )


def _provider_exclusion(txn: Transaction) -> str | None:
    if txn.currency != "USD":
        return "non_usd_currency"
    # Plaid reports money leaving an account as positive. On a card that is a charge.
    # A negative amount is money coming back: a refund, a card payment, or a rewards credit.
    if txn.amount < 0 and PAYMENT_NAME.search(txn.merchant):
        return "payment_or_transfer"
    primary = txn.provider_primary
    # A store return is sometimes labeled income or an incoming transfer. It still reduces
    # the month. A card payment (LOAN_PAYMENTS / TRANSFER_OUT) never does, whatever the merchant.
    if txn.amount < 0 and primary in {"TRANSFER_IN", "INCOME"} and _merchant_return(txn):
        return None
    if primary in CREDIT_ONLY_PRIMARY:
        return "payment_or_transfer" if txn.amount < 0 else None
    if primary in NON_SPEND_PRIMARY:
        return "income_or_credit" if primary == "INCOME" else "payment_or_transfer"
    return None


def resolve(
    txn: Transaction,
    override: Override | None,
    duplicate: str | None,
    rules: Sequence[HouseholdRule] = (),
) -> Resolved:
    if duplicate:
        return Resolved(txn, None, "excluded", duplicate)
    excluded = _provider_exclusion(txn)
    if excluded == "non_usd_currency":
        return Resolved(txn, None, "excluded", excluded)
    if override is not None:
        return Resolved(txn, override.category, "user")
    if excluded:
        return Resolved(txn, None, "excluded", excluded)
    chosen = rule_for(txn.merchant, rules)
    if chosen is not None:
        return Resolved(txn, chosen.category, f"household_rule:{chosen.match}")
    mapped = map_provider_category(txn.provider_primary, txn.provider_detailed)
    # An Amazon return with no useful category still belongs with Amazon purchases.
    if txn.amount < 0 and mapped == Category.MISC and _AMAZON.search(txn.merchant):
        return Resolved(txn, Category.SHOPPING, "refund:amazon")
    ruled = apply_merchant_rules(txn.merchant, mapped)
    if ruled:
        return Resolved(txn, ruled[0], f"merchant_rule:{ruled[1]}")
    source = txn.provider_detailed or txn.provider_primary
    return Resolved(txn, mapped, f"provider:{source}" if source else "default")


def _survivor(txn_id: str, duplicates: Mapping[str, str]) -> str:
    """The record that stands in for a dropped one, following replacements to the end."""
    for _ in range(10):
        reason = duplicates.get(txn_id)
        if reason is None:
            break
        txn_id = reason.split(":", 1)[1]
    return txn_id


class _Dated(Protocol):
    @property
    def set_at(self) -> str: ...


def _following[T: _Dated](
    records: Mapping[str, T], duplicates: Mapping[str, str]
) -> Callable[[Transaction], T | None]:
    """Find the record that applies to a transaction, following the purchase when its identity
    changes: a pending record's record applies to its posted replacement, and a dropped
    duplicate's applies to the record that stands in for it."""
    inherited: dict[str, T] = {}
    for txn_id, record in records.items():
        if txn_id in duplicates:
            heir = _survivor(txn_id, duplicates)
            current = inherited.get(heir)
            if current is None or record.set_at > current.set_at:
                inherited[heir] = record

    def find(txn: Transaction) -> T | None:
        return (
            records.get(txn.txn_id)
            or (records.get(txn.pending_of) if txn.pending_of else None)
            or inherited.get(txn.txn_id)
        )

    return find


def resolve_all(
    transactions: Iterable[Transaction],
    overrides: Mapping[str, Override],
    adjustments: Mapping[str, Adjustment] | None = None,
    rules: Sequence[HouseholdRule] = (),
    splits: Mapping[str, tuple[tuple[Category, Decimal], ...]] | None = None,
) -> list[Resolved]:
    """Resolve every record. A correction or adjustment follows its purchase when the record's
    identity changes.

    People often correct a charge while it is still pending; the posted record then has a new ID
    that names the pending one. The same goes for a CSV row that Plaid later duplicates.
    """
    items = sorted(transactions, key=lambda txn: (txn.spend_date, txn.txn_id))
    duplicates = find_duplicates(items)
    correction = _following(overrides, duplicates)
    adjustment = _following(adjustments or {}, duplicates)
    results: list[Resolved] = []
    for txn in items:
        resolved = resolve(txn, correction(txn), duplicates.get(txn.txn_id), rules)
        adjusted = adjustment(txn)
        # Duplicates were found on the card's own amounts above; only now does the counted
        # amount replace it. An adjustment never raises a charge and never applies to a refund.
        if adjusted is not None and resolved.counted and 0 <= adjusted.counted < txn.amount:
            resolved = replace(
                resolved, txn=replace(txn, amount=adjusted.counted), charged=txn.amount
            )
        split = (splits or {}).get(txn.txn_id)
        if (
            split
            and resolved.counted
            and sum((amount for _, amount in split), Decimal(0)) == resolved.txn.amount
        ):
            largest = max(split, key=lambda part: part[1])
            resolved = replace(resolved, parts=split, category=largest[0])
        results.append(resolved)
    return results
