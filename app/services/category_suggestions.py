"""Category suggestions for a statement import's preview (ab-253).

Pure functions: the endpoint loads rows, the user's rules, the approved
shared list and the user's categories, and this module decides what to
suggest. Nothing is written here.

Order, first hit wins:
  1. your_rule       the user's own memory for this key (user_category_rules)
  2. this_statement  a category the user already gave the same key in this
                     same import
  3. others          what most users chose for this Till/Paybill or business
                     name, once SHARED_MIN_VOTERS+ distinct users voted and
                     one category clearly leads (no admin gate, 2026-11-10);
                     overrides the shared list
  4. directory       an approved shared-list merchant (starter or admin)
Shared categories (3 and 4) are mapped by NAME to the user's own category,
or suggested as a new category when the user has none by that name.
(AI suggestions, ab-256, will slot in after 4.)
"""

import hashlib
import re
from collections import Counter
from collections.abc import Iterable
from dataclasses import dataclass, field, replace
from uuid import UUID

from app.services.merchant_keys import RowKey, extract_key

CATEGORY_TYPE_FOR_DIRECTION = {"out": "expense", "in": "income"}

# Distinct users who must have voted on a merchant before what they chose
# is suggested to anyone else.
SHARED_MIN_VOTERS = 3


# --- shared list ----------------------------------------------------------


def _compile_pattern(pattern: str) -> re.Pattern:
    """Whole-word match; '*' matches any run of characters (including a
    literal '*', as in card descriptors like 'UBER *EATS'). A word boundary
    is only enforced on an edge that is a letter or digit, so 'KFC-' still
    matches 'KFC-WESTGATE'."""
    pattern = pattern.upper().strip()
    parts = pattern.split("*")
    pieces = []
    for i, part in enumerate(parts):
        piece = re.escape(part.strip())
        # A space beside '*' becomes a word boundary rather than a literal
        # space, so 'A * B' matches 'A B' and 'GOOGLE *' not 'GOOGLEPLEX'.
        if i > 0 and part[:1] == " " and part.strip()[:1].isalnum():
            piece = r"(?<![A-Z0-9])" + piece
        if i < len(parts) - 1 and part[-1:] == " " and part.strip()[-1:].isalnum():
            piece += r"(?![A-Z0-9])"
        pieces.append(piece)
    body = ".*?".join(pieces)
    left = r"(?<![A-Z0-9])" if pattern[:1].isalnum() else ""
    right = r"(?![A-Z0-9])" if pattern[-1:].isalnum() else ""
    return re.compile(left + body + right)


@dataclass
class Merchant:
    id: UUID
    display_name: str
    category_name: str
    direction: str
    patterns: list[tuple[int, re.Pattern]] = field(default_factory=list)  # (specificity, regex)

    def text_score(self, text: str) -> int:
        """Length of this merchant's longest pattern found in text, 0 if none."""
        if not text:
            return 0
        return max((length for length, rx in self.patterns if rx.search(text)), default=0)


class Directory:
    def __init__(self, merchants: dict[UUID, Merchant], numbers: dict, blocked: set[tuple[str, str]]):
        self._merchants = merchants
        self._numbers = numbers  # (kind, value, direction) -> (merchant_id, requires_text)
        self._blocked = blocked

    @classmethod
    def from_rows(cls, rows: Iterable[dict], *, blocked: Iterable[dict]) -> "Directory":
        """rows: one per (approved merchant, key), as returned by
        repositories.category_suggestions.get_approved_directory."""
        merchants: dict[UUID, Merchant] = {}
        numbers = {}
        for r in rows:
            m = merchants.setdefault(
                r["merchant_id"],
                Merchant(r["merchant_id"], r["display_name"], r["category_name"], r["direction"]),
            )
            if r["key_kind"] == "name_pattern":
                m.patterns.append((len(r["key_value"].replace("*", "").strip()), _compile_pattern(r["key_value"])))
            else:
                numbers[(r["key_kind"], r["key_value"], r["direction"])] = (r["merchant_id"], r["requires_text"])
        return cls(merchants, numbers, {(b["number_kind"], b["number"]) for b in blocked})

    def is_blocked(self, number_kind: str | None, number: str | None) -> bool:
        return (number_kind, number) in self._blocked

    def merchant_for_number(self, number_kind: str, number: str, direction: str) -> Merchant | None:
        """The merchant a number alone identifies - not blocked, and not a
        number that only counts together with the merchant's name."""
        if (number_kind, number) in self._blocked:
            return None
        hit = self._numbers.get((number_kind, number, direction))
        if not hit or hit[1]:
            return None
        return self._merchants[hit[0]]

    def owns_number(self, number_kind: str, number: str, direction: str) -> bool:
        """The list already has this number as a key (alone or with text)."""
        return (number_kind, number, direction) in self._numbers

    def match(self, key: RowKey, direction: str) -> Merchant | None:
        if key.number and (key.number_kind, key.number) not in self._blocked:
            hit = self._numbers.get((key.number_kind, key.number, direction))
            if hit:
                merchant = self._merchants[hit[0]]
                if not hit[1] or merchant.text_score(key.match_text):
                    return merchant
        return self.match_text(key.match_text, direction)

    def match_text(self, text: str, direction: str) -> Merchant | None:
        """The merchant whose longest name pattern is found in text."""
        if not text:
            return None
        best, best_score = None, 0
        for m in self._merchants.values():
            if m.direction != direction:
                continue
            score = m.text_score(text)
            if score > best_score:
                best, best_score = m, score
        return best


# --- the user's own rules -------------------------------------------------


def pick_rule(rules: list[dict], *, account_hash: str | None, amount: float) -> dict | None:
    """rules: the user's rules for one key and direction, one per category.

    Same account reference first. A different (or no) account on a
    Paybill the user has used for several categories gives nothing - e.g.
    a bank Paybill used for both rent and school fees. Otherwise the most
    used category, ties broken by the closest average amount; still tied
    means no suggestion rather than a coin toss."""
    if not rules:
        return None
    same_account = [r for r in rules if account_hash and r["account_hash"] == account_hash]
    if same_account:
        rules = same_account
    elif len({r["category_id"] for r in rules}) > 1 and any(r["account_hash"] for r in rules):
        return None

    totals: dict = {}
    for r in rules:
        t = totals.setdefault(r["category_id"], {"rule": r, "times_used": 0})
        t["times_used"] += r["times_used"]
    ranked = sorted(totals.values(), key=lambda t: t["times_used"], reverse=True)
    top = [t for t in ranked if t["times_used"] == ranked[0]["times_used"]]
    if len(top) == 1:
        return top[0]["rule"]

    def distance(t):
        avg = t["rule"]["avg_amount"]
        return abs(float(avg) - float(amount)) if avg is not None else None

    distances = [(distance(t), t) for t in top]
    if any(d is None for d, _ in distances):
        return None
    distances.sort(key=lambda dt: dt[0])
    if distances[0][0] == distances[1][0]:
        return None
    return distances[0][1]["rule"]


# --- the preview ----------------------------------------------------------


def extract_keys(rows: list[dict], *, provider: str, secret: str) -> dict[UUID, RowKey]:
    return {
        r["id"]: extract_key(provider=provider, description=r["description"], direction=r["direction"], secret=secret)
        for r in rows
    }


def rule_key(key: RowKey, directory: "Directory") -> tuple[str, str, str | None] | None:
    """(key_kind, key_value, provider) a user's rule for this row is stored
    under - the same for preview lookups and confirm learning.

    A blocked number (shared collection Paybill like 222222 eCitizen)
    never keys a rule on its own: the rule uses the row's name instead,
    so one eCitizen payment doesn't decide every other one. provider is
    kept for reference keys only."""
    if not key.kind:
        return None
    if key.kind in ("paybill", "till") and directory.is_blocked(key.number_kind, key.number):
        return ("name", key.name, None) if key.name else None
    return (key.kind, key.value, key.provider if key.kind == "reference" else None)


def rule_lookup_pairs(keys: Iterable[RowKey], directory: "Directory") -> list[tuple[str, str]]:
    """Distinct (key_kind, key_value) pairs to fetch the user's rules for."""
    pairs = {(rk[0], rk[1]) for k in keys if (rk := rule_key(k, directory))}
    return sorted(pairs)


def vote_keys(key: RowKey, direction: str, directory: "Directory") -> tuple[tuple | None, tuple | None]:
    """(name_key, number_key) a row votes under and reads others' votes by -
    the same for confirm (votes) and preview (others).

    name_key   ('name', business name, direction): business-looking names only
    number_key (kind, number, direction): a Paybill/Till that isn't blocked,
               on a business-looking or nameless row
    A Till under a person's name, and reference rows (transfers, NCBA
    account references), never vote or read votes."""
    if not key.kind or key.kind == "reference":
        return None, None
    name_key = ("name", key.name, direction) if key.is_business and key.name else None
    number_key = (
        (key.number_kind, key.number, direction)
        if key.number
        and not directory.is_blocked(key.number_kind, key.number)
        and (key.is_business or not key.name)
        else None
    )
    return name_key, number_key


def shared_vote_lookups(
    rows: list[dict], keys: dict[UUID, RowKey], directory: "Directory"
) -> tuple[list[tuple], list[tuple]]:
    """Distinct (name_keys, number_keys) to load vote totals for."""
    names, numbers = set(), set()
    for row in rows:
        name_key, number_key = vote_keys(keys[row["id"]], row["direction"], directory)
        if name_key:
            names.add(name_key)
        if number_key:
            numbers.add(number_key)
    return sorted(names), sorted(numbers)


def pick_shared(votes: list[dict]) -> str | None:
    """votes: [{category_name, users}] for one merchant. The category most
    distinct users chose, once SHARED_MIN_VOTERS+ voted and it clearly
    leads (a tie, or nobody agreeing, gives nothing - so one person's own
    category name never spreads)."""
    if sum(v["users"] for v in votes) < SHARED_MIN_VOTERS:
        return None
    ranked = sorted(votes, key=lambda v: v["users"], reverse=True)
    if ranked[0]["users"] < 2 or (len(ranked) > 1 and ranked[1]["users"] == ranked[0]["users"]):
        return None
    return ranked[0]["category_name"]


# --- shared Till/Paybill -> name learning (ab-253 PR 4) ---------------------

LEARNED_NAME_MIN_USERS = 1  # the name is printed by Safaricom, not chosen by the user
KEY_GROWTH_MIN_USERS = 3  # before a number is added to a listed merchant


def _is_number_key(key: RowKey) -> bool:
    return key.kind in ("paybill", "till") and bool(key.number)


def name_sightings(rows: list[dict], keys: dict[UUID, RowKey], directory: Directory) -> list[dict]:
    """Number -> name pairs a confirm records for this user: rows printing a
    Paybill/Till with a business name (business-looking, or a name on the
    list). Never person-named Tills, blocked numbers, references, or names
    this row only borrowed. One per number+direction - the latest name."""
    seen: dict[tuple, tuple] = {}
    for row in rows:
        key, direction = keys[row["id"]], row["direction"]
        if not _is_number_key(key) or not key.name or key.name_learned:
            continue
        if directory.is_blocked(key.number_kind, key.number):
            continue
        if not (key.is_business or directory.match_text(key.name, direction)):
            continue
        k = (key.number_kind, key.number, direction)
        if k not in seen or row["txn_date"] >= seen[k][0]:
            seen[k] = (row["txn_date"], key.name)
    return [{"number_kind": k[0], "number": k[1], "direction": k[2], "name": name} for k, (_, name) in seen.items()]


def _wants_learned_name(key: RowKey, direction: str, directory: Directory) -> bool:
    return (
        _is_number_key(key)
        and not key.name
        and not directory.is_blocked(key.number_kind, key.number)
        and not directory.owns_number(key.number_kind, key.number, direction)
    )


def learned_name_lookups(rows: list[dict], keys: dict[UUID, RowKey], directory: Directory) -> list[tuple]:
    """(number_kind, number, direction) of rows showing only a number the
    list doesn't know - the ones that can borrow a learned name."""
    return sorted({
        (keys[r["id"]].number_kind, keys[r["id"]].number, r["direction"])
        for r in rows
        if _wants_learned_name(keys[r["id"]], r["direction"], directory)
    })


def pick_learned_name(names: list[dict]) -> dict | None:
    """names: [{name, users, last_seen}] for one number. The name the most
    distinct users saw; a tie goes to the most recently seen."""
    if not names:
        return None
    top = max(names, key=lambda n: (n["users"], n["last_seen"]))
    return top if top["users"] >= LEARNED_NAME_MIN_USERS else None


def apply_learned_names(
    rows: list[dict], keys: dict[UUID, RowKey], learned: dict[tuple, list[dict]], directory: Directory
) -> dict[UUID, RowKey]:
    """New keys with learned names filled in. The row's rule identity
    (kind/value) is unchanged, so the user's own memory isn't affected;
    the name drives display, list matching and name votes."""
    out = dict(keys)
    for row in rows:
        key, direction = keys[row["id"]], row["direction"]
        if not _wants_learned_name(key, direction, directory):
            continue
        top = pick_learned_name(learned.get((key.number_kind, key.number, direction), []))
        if top:
            out[row["id"]] = replace(key, name=top["name"], match_text=top["name"], is_business=True, name_learned=True)
    return out


def plan_key_growth(learned: dict[tuple, list[dict]], directory: Directory) -> list[dict]:
    """Numbers to add to a listed merchant: KEY_GROWTH_MIN_USERS+ distinct
    users saw it under a top name matching that merchant, and the list
    doesn't already own (or block) the number."""
    out = []
    for (kind, number, direction), names in sorted(learned.items()):
        top = pick_learned_name(names)
        if not top or top["users"] < KEY_GROWTH_MIN_USERS:
            continue
        if directory.is_blocked(kind, number) or directory.owns_number(kind, number, direction):
            continue
        merchant = directory.match_text(top["name"], direction)
        if merchant:
            out.append({"merchant_id": merchant.id, "direction": direction, "key_kind": kind, "key_value": number})
    return out


def group_key(key: RowKey, direction: str) -> str | None:
    """Opaque id shared by rows the UI should treat as the same payee
    (pick a category for one, offer it for the rest). Hashed so names,
    numbers and references don't appear in it."""
    if not key.kind:
        return None
    raw = "|".join([key.kind, key.value or "", key.provider or "", key.account_hash or "", direction])
    return "g:" + hashlib.sha256(raw.encode()).hexdigest()[:16]


def _is_open(row: dict) -> bool:
    return not row["is_transfer"] and float(row["allocated"] or 0) == 0


def build_preview_rows(
    rows: list[dict],
    *,
    keys: dict[UUID, RowKey],
    rules: list[dict],
    directory: Directory,
    categories: list[dict],
    shared_votes: dict[tuple, list[dict]] | None = None,
) -> list[dict]:
    """rows: every transaction in the import, each with its allocated
    total and (when its allocations use exactly one category) category_id.
    shared_votes: vote totals keyed by vote_keys() tuples.
    Returns the open rows - nothing allocated yet, not a transfer - each
    with its suggestion or None."""
    shared_votes = shared_votes or {}
    by_id = {c["id"]: c for c in categories}
    by_name = {(c["name"].strip().lower(), c["type"]): c for c in categories}

    rules_by_key: dict[tuple, list[dict]] = {}
    for r in rules:
        rules_by_key.setdefault((r["key_kind"], r["key_value"], r["provider"], r["direction"]), []).append(r)

    # Categories already chosen in this import, per group.
    chosen: dict[str, Counter] = {}
    for row in rows:
        g = group_key(keys[row["id"]], row["direction"])
        if g and row["category_id"] and not _is_open(row):
            chosen.setdefault(g, Counter())[row["category_id"]] += 1

    out = []
    for row in rows:
        if not _is_open(row):
            continue
        key = keys[row["id"]]
        direction = row["direction"]
        wanted_type = CATEGORY_TYPE_FOR_DIRECTION[direction]
        g = group_key(key, direction)
        suggestion, label, known_as = None, None, None
        # A row showing only a number the list owns is named after that
        # merchant, whichever source the suggestion comes from.
        listed = _is_number_key(key) and not key.name and directory.merchant_for_number(
            key.number_kind, key.number, direction
        )
        if listed:
            known_as = listed.display_name

        rk = rule_key(key, directory)
        if rk:
            candidates = [
                r for r in rules_by_key.get((*rk, direction), [])
                if r["category_id"] in by_id and by_id[r["category_id"]]["type"] == wanted_type
            ]
            picked = pick_rule(candidates, account_hash=key.account_hash, amount=row["amount"])
            if picked:
                label = next((r["label"] for r in candidates if r["label"]), None)
                suggestion = _existing(by_id[picked["category_id"]], "your_rule")
                # A Paybill the user typed for a reference row names the
                # merchant for them, if the shared list knows it.
                typed = next((r.get("user_entered_paybill") for r in candidates if r.get("user_entered_paybill")), None)
                merchant = typed and directory.merchant_for_number("paybill", typed, direction)
                if merchant:
                    known_as = merchant.display_name
                    suggestion["merchant_name"] = merchant.display_name

        if suggestion is None and g in chosen:
            ranked = chosen[g].most_common(2)
            if len(ranked) == 1 or ranked[0][1] > ranked[1][1]:
                category = by_id.get(ranked[0][0])
                if category and category["type"] == wanted_type:
                    suggestion = _existing(category, "this_statement")

        if suggestion is None:
            name_key, number_key = vote_keys(key, direction, directory)
            for vote_key in (number_key, name_key):
                chosen_name = vote_key and pick_shared(shared_votes.get(vote_key, []))
                if chosen_name:
                    suggestion = _shared(chosen_name, by_name, wanted_type, "others")
                    break

        if suggestion is None:
            merchant = directory.match(key, direction)
            if merchant:
                suggestion = _shared(merchant.category_name, by_name, wanted_type, "directory")
                suggestion["merchant_name"] = merchant.display_name

        out.append({
            "transaction_id": row["id"],
            "txn_date": row["txn_date"],
            "amount": row["amount"],
            "currency": row["currency"],
            "direction": direction,
            "description": row["description"],
            "display_name": label or key.name or known_as or key.display or row["description"] or "",
            "label": label,
            "group_key": g,
            "key_kind": key.kind,
            "number_kind": key.number_kind,
            "number": key.number,
            "is_business": key.is_business,
            "is_transfer_hint": key.is_transfer_hint,
            "name_learned": key.name_learned,
            "row_kind": key.row_kind,
            "paid_with_overdraft": key.paid_with_overdraft,
            "suggestion": suggestion,
        })
    return out


def _shared(category_name: str, by_name: dict, wanted_type: str, source: str) -> dict:
    """A shared category name mapped to the user's own category (ignoring
    case, same type), or suggested as a new one."""
    category = by_name.get((category_name.strip().lower(), wanted_type))
    if category:
        return _existing(category, source)
    return {"category_id": None, "category_name": category_name.strip(), "is_new_category": True,
            "source": source, "merchant_name": None}


def _existing(category: dict, source: str) -> dict:
    return {
        "category_id": category["id"],
        "category_name": category["name"],
        "is_new_category": False,
        "source": source,
        "merchant_name": None,
    }


def new_category_names(preview_rows: list[dict]) -> list[str]:
    """Distinct (ignoring case) names of categories the suggestions would
    create - for the "Includes K new categories: ..." line."""
    seen: dict[str, str] = {}
    for r in preview_rows:
        s = r["suggestion"]
        if s and s["is_new_category"]:
            seen.setdefault(s["category_name"].strip().lower(), s["category_name"].strip())
    return sorted(seen.values(), key=str.lower)
