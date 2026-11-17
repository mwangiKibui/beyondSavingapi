"""Rows grouped by payee (ab-282), for Transactions -> Uncategorised (every
statement, every month) and for one import's review tabs.

Pure: callers load the rows and their keys. Each row is keyed against its
OWN account's provider, so one list can mix M-Pesa and bank statements.
Rows the same payee paid or was paid by share a group (see group_key);
rows with no readable payee are groups of one. Person rows show the
statement text, and only ever to the user who imported them (ab-269).
"""

from collections import Counter
from uuid import UUID

from app.services.category_suggestions import _display_name, _is_open, group_key
from app.services.merchant_keys import RowKey, extract_key

ALLOWED_PAGE_SIZES = (5, 10, 20, 30)
DEFAULT_PAGE_SIZE = 30
SORTS = ("rows", "amount", "payee")
REVIEW_TABS = ("decide", "decided", "sorted", "handled")


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


def review_tab(row: dict) -> str:
    """Which review tab an import's row sits in:
    decide   still open
    decided  categorised by an apply from this import's review
    sorted   categorised (or reconciled) some other way, e.g. on an
             earlier, overlapping statement
    handled  handled at import (ab-281)"""
    if row.get("handled_as"):
        return "handled"
    if _is_open(row):
        return "decide"
    return "decided" if row.get("applied_here") else "sorted"


def _suggestion_id(s: dict) -> tuple:
    return (s["category_id"], s["category_name"].strip().lower())


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
) -> list[dict]:
    """rows: each with id, txn_date, amount, currency, direction,
    description, import_id, file_name, account_nickname and, where known,
    category_id / apply_id / handled_as / handled_reason.
    preview: build_preview_rows output by transaction id (suggestion, label)
    for open rows. Groups come back in first-seen order."""
    preview = preview or {}
    category_names = category_names or {}
    by_key: dict[str, list[dict]] = {}
    for r in rows:
        k = payee_group_key(keys[r["id"]], r["direction"], r["id"])
        if r.get("handled_as"):
            k = f"{r['handled_as']}:{k}"
        by_key.setdefault(k, []).append(r)

    groups = []
    for gk, members in by_key.items():
        members = sorted(members, key=lambda r: (r["txn_date"], str(r["id"])))
        latest = members[-1]
        key = keys[latest["id"]]
        known_as = preview.get(latest["id"], {}).get("display_name")
        statements: dict = {}
        for r in members:
            if r.get("import_id") is not None:
                statements.setdefault(r["import_id"], {
                    "import_id": r["import_id"],
                    "file_name": r.get("file_name"),
                    "account_nickname": r.get("account_nickname"),
                })
        category_id = _same([r.get("category_id") for r in members])
        groups.append({
            "group_key": gk,
            "display_name": known_as or _display_name(key, None, latest["description"]),
            "label": next((preview[r["id"]].get("label") for r in members
                           if r["id"] in preview and preview[r["id"]].get("label")), None),
            "key_kind": key.kind,
            "number_kind": key.number_kind,
            "number": key.number,
            "is_person": key.is_person,
            "is_business": key.is_business,
            "direction": latest["direction"],
            "currency": latest["currency"],
            "row_count": len(members),
            "total": round(sum(float(r["amount"]) for r in members), 2),
            "first_date": members[0]["txn_date"],
            "last_date": latest["txn_date"],
            "statements": list(statements.values()),
            "suggestion": group_suggestion([preview.get(r["id"], {}).get("suggestion") for r in members]),
            "category_id": category_id,
            "category_name": category_names.get(category_id) if category_id else None,
            "apply_id": _same([r.get("apply_id") for r in members]),
            "handled_as": latest.get("handled_as"),
            "handled_reason": _same([r.get("handled_reason") for r in members]),
            "rows": [
                {
                    "transaction_id": r["id"],
                    "txn_date": r["txn_date"],
                    "amount": float(r["amount"]),
                    "currency": r["currency"],
                    "description": r["description"],
                    "import_id": r.get("import_id"),
                }
                for r in members
            ],
        })
    return groups


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
) -> tuple[list[dict], int]:
    """Filter (money in/out; people/businesses; search), sort, then one
    page. Returns (page of groups, total groups after filtering)."""
    needle = (q or "").strip().lower()
    out = [
        g for g in groups
        if (direction is None or g["direction"] == direction)
        and (kind is None or (kind == "person") == bool(g["is_person"]))
        and (not needle or _matches(g, needle))
    ]
    if sort == "payee":
        sort_key = lambda g: (g["display_name"] or "").lower()  # noqa: E731
    elif sort == "amount":
        sort_key = lambda g: g["total"]  # noqa: E731
    else:
        sort_key = lambda g: (g["row_count"], g["total"])  # noqa: E731
    out.sort(key=lambda g: g["group_key"])  # stable tie-break
    out.sort(key=sort_key, reverse=order == "desc")
    start = (page - 1) * page_size
    return out[start:start + page_size], len(out)
