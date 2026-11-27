"""Rows grouped by payee (ab-282), for Transactions -> Uncategorised (every
statement, every month) and for one import's review tabs.

Pure: callers load the rows and their keys. Each row is keyed against its
OWN account's provider, so one list can mix M-Pesa and bank statements.
Rows the same payee paid or was paid by share a group (see group_key);
rows with no readable payee are groups of one. Person rows show the
statement text, and only ever to the user who imported them (ab-269).
"""

import hashlib
from collections import Counter
from datetime import datetime
from uuid import UUID

from app.services.row_kind import allocation_kind
from app.services.category_suggestions import _display_name, _is_open, group_key
from app.services.merchant_keys import PERSON, RowKey, extract_key, person_display

ALLOWED_PAGE_SIZES = (5, 10, 20, 30)
DEFAULT_PAGE_SIZE = 30
SORTS = ("rows", "amount", "payee", "recent")
REVIEW_TABS = ("decide", "decided", "sorted", "handled", "accepted")
# ab-335: the Allocation step's cards, by row.
CARDS = ("pending", "suggested", "approved")


def extract_row_keys(rows: list[dict], *, secret: str) -> dict[UUID, RowKey]:
    """Like category_suggestions.extract_keys, but each row brings its own
    provider (rows from several accounts)."""
    return {
        r["id"]: extract_key(provider=r["provider"], description=r["description"], direction=r["direction"],
                             secret=secret)
        for r in rows
    }


def payee_group_key(key: RowKey, direction: str, transaction_id: UUID) -> str:
    """The row's payee group; a row with no payee is a group of its own."""
    return group_key(key, direction) or f"t:{transaction_id}"


def person_group_key(key: RowKey) -> str | None:
    """ab-343: shared by a person's money-in and money-out groups when the
    rows carry their phone number, so the list shows them as one person.
    Never set from a name alone (two Janes with different numbers are two
    people). Opaque: the number never appears in it."""
    if not key.is_person or key.provider != PERSON or not key.person_number:
        return None
    return "p:" + hashlib.sha256(f"person|{key.value}".encode()).hexdigest()[:16]


def review_tab(row: dict) -> str:
    """Which review tab an import's row sits in:
    decide   still open
    decided  categorised by an apply from this import's review
    sorted   categorised (or reconciled) some other way, e.g. on an
             earlier, overlapping statement
    handled  handled at import (ab-281) - not Saving / Borrowed the user
             filed in the review (ab-288), which carry an apply
    accepted handled at import, and the user accepted it (ab-335)"""
    if _handled_at_import(row):
        return "accepted" if row.get("handling_accepted_at") else "handled"
    if _is_open(row):
        return "decide"
    return "decided" if row.get("applied_here") else "sorted"


def _handled_at_import(row: dict) -> bool:
    return bool(row.get("handled_as")) and row.get("apply_id") is None


# ab-288: the review's names for row kinds (app.services.row_kind).
TYPE_FOR_KIND = {"spending": "expense"}


def row_type(row: dict, *, category_types: dict, category_entry_types: dict) -> str | None:
    """What a filed row is, as the review's Allocation column names it:
    expense, income, refund, transfer, saving, borrowed, loan_repayment.
    None while the row is still open."""
    filed = row.get("handled_as") or row.get("category_id") or row.get("transfer_reason_id") or row.get("apply_id")
    if not filed:
        return None
    category_id = row.get("category_id")
    kind = allocation_kind(
        direction=row["direction"],
        handled_as=row.get("handled_as"),
        has_category=category_id is not None,
        entry_type=category_entry_types.get(category_id),
        has_transfer_reason=row.get("transfer_reason_id") is not None,
        category_type=category_types.get(category_id),
    )
    return TYPE_FOR_KIND.get(kind, kind)


def other_side(row: dict) -> str | None:
    """Where a transfer's money went / came from: the paired row's account,
    else the account or name the user gave."""
    if row.get("transfer_reason_id") is None and not row.get("paired_account_nickname"):
        return None
    return row.get("paired_account_nickname") or row.get("source_account_name") or row.get("source_description")


def _suggestion_id(s: dict) -> tuple:
    # A Funds transfer (ab-285) never merges with a category of the same name.
    return (s.get("kind", "category"), s["category_id"], s["category_name"].strip().lower())


def group_suggestion(suggestions: list[dict | None]) -> dict | None:
    """The suggestion most of the group's rows got; a tie gives none, so
    one click never files rows under a coin toss."""
    counts = Counter(_suggestion_id(s) for s in suggestions if s)
    if not counts:
        return None
    ranked = counts.most_common(2)
    if len(ranked) > 1 and ranked[0][1] == ranked[1][1]:
        return None
    return next(s for s in suggestions if s and _suggestion_id(s) == ranked[0][0])


def _same(values: list):
    first = values[0]
    return first if all(v == first for v in values) else None


def build_groups(
    rows: list[dict],
    *,
    keys: dict[UUID, RowKey],
    preview: dict[UUID, dict] | None = None,
    category_names: dict[UUID, str] | None = None,
    with_status: bool = False,
    category_types: dict[UUID, str] | None = None,
    category_entry_types: dict[UUID, str] | None = None,
) -> list[dict]:
    """rows: each with id, txn_date, amount, currency, direction,
    description, import_id, file_name, account_nickname and, where known,
    category_id / apply_id / handled_as / handled_reason.
    preview: build_preview_rows output by transaction id (suggestion, label)
    for open rows. Groups come back in first-seen order.
    with_status (an import's review, ab-289/ab-292): each line gets its
    review status, and the group its status and pending/approved counts;
    a payee's pending and approved lines share one group."""
    preview = preview or {}
    category_names = category_names or {}
    category_types = category_types or {}
    category_entry_types = category_entry_types or {}

    def type_of(r: dict) -> str | None:
        return row_type(r, category_types=category_types, category_entry_types=category_entry_types)

    def is_refund(r: dict) -> bool:
        # ab-286: money in under an expense category.
        return r["direction"] == "in" and category_types.get(r.get("category_id")) == "expense"

    by_key: dict[str, list[dict]] = {}
    for r in rows:
        k = payee_group_key(keys[r["id"]], r["direction"], r["id"])
        if _handled_at_import(r):
            k = f"{r['handled_as']}:{k}"
        by_key.setdefault(k, []).append(r)

    # ab-341: a person's name from any row with the same key (e.g. a bank's
    # incoming row names the number its outgoing rows only show).
    person_names: dict[str, tuple[str, str | None]] = {}
    for r in rows:
        k = keys.get(r["id"])
        if k is not None and k.is_person and k.person_name and k.value not in person_names:
            person_names[k.value] = (k.person_name, k.person_number)

    groups = []
    for gk, members in by_key.items():
        members = sorted(members, key=lambda r: (r["txn_date"], str(r["id"])))
        latest = members[-1]
        key = keys[latest["id"]]
        known_as = preview.get(latest["id"], {}).get("display_name")
        if key.is_person and key.value in person_names:
            name, number = person_names[key.value]
            known_as = person_display(name, number or key.person_number)
        statements: dict = {}
        for r in members:
            if r.get("import_id") is not None:
                statements.setdefault(r["import_id"], {
                    "import_id": r["import_id"],
                    "file_name": r.get("file_name"),
                    "account_nickname": r.get("account_nickname"),
                })
        category_id = _same([r.get("category_id") for r in members])
        statuses = [review_tab(r) for r in members] if with_status else []
        groups.append({
            "group_key": gk,
            "display_name": known_as or _display_name(key, None, latest["description"]),
            "label": next((preview[r["id"]].get("label") for r in members
                           if r["id"] in preview and preview[r["id"]].get("label")), None),
            "key_kind": key.kind,
            "number_kind": key.number_kind,
            "number": key.number,
            "is_person": key.is_person,
            # ab-343: rows handled at import stay on their own.
            "person_key": None if _handled_at_import(latest) else person_group_key(key),
            "is_business": key.is_business,
            "direction": latest["direction"],
            "currency": latest["currency"],
            "row_count": len(members),
            "total": round(sum(float(r["amount"]) for r in members), 2),
            "first_date": members[0]["txn_date"],
            "last_date": latest["txn_date"],
            "statements": list(statements.values()),
            "suggestion": group_suggestion([preview.get(r["id"], {}).get("suggestion") for r in members]),
            # ab-345: some open row still waits on an AI answer.
            "ai_missing": any(preview.get(r["id"], {}).get("ai_missing") for r in members),
            "category_id": category_id,
            "category_name": category_names.get(category_id) if category_id else None,
            "is_refund": bool(category_id) and is_refund(latest),
            "kind": _same([type_of(r) for r in members]),
            "sub_ledger_id": _same([r.get("sub_ledger_id") for r in members]),
            "sub_ledger_name": _same([r.get("sub_ledger_name") for r in members]),
            "sub_ledger_role": _same([r.get("sub_ledger_role") for r in members]),
            "other_side": _same([other_side(r) for r in members]),
            # ab-340: the account or name the user gave, so Edit starts on it.
            "other_account_id": _same([r.get("source_account_id") for r in members]),
            "someone_else_name": _same([r.get("source_description") for r in members]),
            # ab-285: every row filed as a Funds transfer under one reason.
            "transfer_reason_id": _same([r.get("transfer_reason_id") for r in members]),
            "transfer_reason_name": _same([r.get("transfer_reason_name") for r in members]),
            "apply_id": _same([r.get("apply_id") for r in members]),
            "handled_as": latest.get("handled_as"),
            "handled_reason": _same([r.get("handled_reason") for r in members]),
            **(_group_status(statuses) if with_status else {}),
            "rows": [
                {
                    "transaction_id": r["id"],
                    "txn_date": r["txn_date"],
                    "amount": float(r["amount"]),
                    "currency": r["currency"],
                    "description": r["description"],
                    "import_id": r.get("import_id"),
                    # ab-296: the upload that first recorded the row, so a
                    # re-upload's Review can say "already recorded from ...".
                    "file_name": r.get("file_name"),
                    "imported_at": r.get("import_created_at"),
                    "status": statuses[i] if with_status else None,
                    "category_id": r.get("category_id"),
                    "category_name": category_names.get(r.get("category_id")) if r.get("category_id") else None,
                    "transfer_reason_id": r.get("transfer_reason_id"),
                    "transfer_reason_name": r.get("transfer_reason_name"),
                    "is_refund": is_refund(r),
                    "kind": type_of(r),
                    # ab-288: which sub-ledger the row is on (Saving on money
                    # in, "Set to Locked deposits"), and a transfer's other
                    # side - only ever shown to the importer.
                    "account_id": r.get("account_id"),
                    "sub_ledger_id": r.get("sub_ledger_id"),
                    "sub_ledger_name": r.get("sub_ledger_name"),
                    "sub_ledger_role": r.get("sub_ledger_role"),
                    "other_side": other_side(r),
                    "other_account_id": r.get("source_account_id"),
                    "someone_else_name": r.get("source_description"),
                    "note": r.get("note"),
                    "apply_id": r.get("apply_id"),
                    "suggestion": preview.get(r["id"], {}).get("suggestion"),
                }
                for i, r in enumerate(members)
            ],
        })
    return groups


def _group_status(statuses: list[str]) -> dict:
    """A group is Pending while any line is (ab-292); otherwise its lines'
    shared status, or Approved (decided before sorted)."""
    pending = statuses.count("decide")
    approved = statuses.count("decided") + statuses.count("sorted") + statuses.count("accepted")
    if pending:
        status = "decide"
    elif "handled" in statuses:
        status = "handled"
    elif "accepted" in statuses:
        status = "accepted"
    else:
        status = "decided" if "decided" in statuses else "sorted"
    return {"status": status, "statuses": set(statuses), "pending_count": pending, "approved_count": approved}


def row_card(group: dict, row: dict) -> str:
    """ab-335: which Allocation card a group's row counts under.
    pending    open, nothing suggested
    suggested  open with a suggestion (the row's own or its payee group's,
               as the Approve button uses), or handled at import and not
               accepted yet
    approved   allocated (here or elsewhere) or an accepted handled row"""
    status = row.get("status")
    if status == "decide":
        return "suggested" if row.get("suggestion") or group.get("suggestion") else "pending"
    if status == "handled":
        return "suggested"
    return "approved"


def group_cards(group: dict) -> set[str]:
    """The cards a payee group has rows under (a group can span two)."""
    return {row_card(group, r) for r in group.get("rows") or []}


def card_counts(groups: list[dict]) -> dict[str, int]:
    """Rows per card, across every group."""
    counts = dict.fromkeys(CARDS, 0)
    for g in groups:
        for r in g.get("rows") or []:
            counts[row_card(g, r)] += 1
    return counts


def _matches(group: dict, needle: str) -> bool:
    """Search (ab-283): payee name, the user's label, the payee's number or
    any statement line in the group. Runs on the importer's own rows only."""
    texts = [group.get("display_name"), group.get("label"), group.get("number")]
    texts += [r.get("description") for r in group.get("rows") or []]
    return any(needle in t.lower() for t in texts if t)


def select_groups(
    groups: list[dict],
    *,
    direction: str | None = None,
    kind: str | None = None,
    q: str | None = None,
    sort: str = "rows",
    order: str = "desc",
    page: int = 1,
    page_size: int = DEFAULT_PAGE_SIZE,
    handled_as: str | None = None,
) -> tuple[list[dict], int]:
    """Filter (money in/out; people/businesses; how it was handled;
    search), sort, then one page. A group's status never moves it
    (2026-11-18): a payee the user files stays where it is, and the
    Pending filter is how to see only what's left. Returns (page of
    groups, total groups after filtering)."""
    needle = (q or "").strip().lower()
    out = [
        g for g in groups
        if (direction is None or g["direction"] == direction)
        and (kind is None or (kind == "person") == bool(g["is_person"]))
        and (handled_as is None or g.get("handled_as") == handled_as)
        and (not needle or _matches(g, needle))
    ]
    # ab-343: a person's money-in and money-out groups sort, page and count
    # as one (out first); every other group is a bundle of one.
    bundles: dict[str, list[dict]] = {}
    for g in out:
        bundles.setdefault(_bundle_id(g), []).append(g)
    ordered = [sorted(b, key=lambda g: (g["direction"] != "out", g["group_key"])) for b in bundles.values()]
    ordered.sort(key=lambda b: min(g["group_key"] for g in b))  # stable tie-break
    ordered.sort(key=lambda b: _bundle_sort_key(b, sort), reverse=order == "desc")
    start = (page - 1) * page_size
    return [g for b in ordered[start:start + page_size] for g in b], len(ordered)


def _bundle_id(group: dict) -> str:
    return group.get("person_key") or f"g:{group['group_key']}"


def count_payees(groups: list[dict]) -> int:
    """ab-343: payees as the list shows them - a person's money in and out
    count once, like select_groups' total."""
    return len({_bundle_id(g) for g in groups})


def _recent(group: dict):
    # ab-284: newest filed first ("Categorised here"), else latest row. A
    # filed time and a row date compare by day first.
    value = group.get("applied_at") or group["last_date"]
    day = value.date() if isinstance(value, datetime) else value
    return day, value.isoformat()


def _bundle_sort_key(bundle: list[dict], sort: str):
    if sort == "payee":
        return (bundle[0]["display_name"] or "").lower()
    if sort == "amount":
        return sum(g["total"] for g in bundle)
    if sort == "recent":
        return max(_recent(g) for g in bundle)
    return sum(g["row_count"] for g in bundle), sum(g["total"] for g in bundle)
