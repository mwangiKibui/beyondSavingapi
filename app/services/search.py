"""The navbar search (ab-274): query handling and result items. Pure, no I/O.

Everything here works on rows the repository already scoped to the signed-in
user. Payees reuse the payee-group keys (ab-282) on the rows whose text
matched, so a person's name is only ever shown to the user who imported it
(ab-269) and the shared merchant directory is never read.
"""

import re
from datetime import timedelta, timezone
from decimal import Decimal, InvalidOperation

from app.services.category_suggestions import _display_name
from app.services.payee_groups import extract_row_keys, payee_group_key

MIN_QUERY_LENGTH = 2
MAX_QUERY_LENGTH = 100

_AMOUNT = re.compile(r"^(?:KES|KSH|KSHS)?\.?\s*(\d[\d,\s]*(?:\.\d{1,2})?)$", re.IGNORECASE)
# Plans are stored as local (EAT) midnights; show the month the user picked.
_EAT = timezone(timedelta(hours=3))
_ACCOUNT_TYPES = {"bank": "Bank", "mobile_money": "Mobile money", "sacco": "SACCO", "money_market": "Money market"}


def normalise_query(q: str | None) -> str | None:
    """Trimmed, inner whitespace collapsed; None when too short to search."""
    text = " ".join((q or "").split())
    return text if len(text) >= MIN_QUERY_LENGTH else None


def parse_amount(q: str) -> Decimal | None:
    """'2600', '2,600', 'KES 2,600.50' -> the amount; None if q isn't one."""
    m = _AMOUNT.match(q.strip())
    if not m:
        return None
    try:
        return Decimal(re.sub(r"[,\s]", "", m.group(1)))
    except InvalidOperation:
        return None


def like_pattern(q: str) -> str:
    """A partial-match ILIKE pattern: q's words in order, anything between
    them (so 'java house' matches 'JAVA  HOUSE' however the statement
    spaced it), with q's own wildcards escaped."""
    words = [w.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_") for w in q.split()]
    return "%" + "%".join(words) + "%"


def _day(d) -> str:
    return f"{d.day} {d:%b %Y}"


def _money(amount, currency: str, direction: str) -> str:
    sign = "−" if direction == "out" else "+"
    return f"{sign}{currency.strip()} {float(amount):,.2f}"


def transaction_item(r: dict) -> dict:
    title = " ".join((r.get("description") or r.get("counterparty") or "").split()) or "Transaction"
    return {
        "type": "transaction",
        "id": str(r["id"]),
        "title": title,
        "subtitle": f"{_day(r['txn_date'])} · {r['account_nickname']} · "
                    f"{_money(r['amount'], r['currency'], r['direction'])}",
        "link": {"transaction_id": str(r["id"]), "account_id": str(r["account_id"])},
        "txn_date": r["txn_date"],
        "amount": float(r["amount"]),
        "currency": r["currency"].strip(),
        "direction": r["direction"],
    }


def payee_items(rows: list[dict], *, q: str, secret: str, limit: int, offset: int) -> tuple[list[dict], int]:
    """rows: the user's statement rows whose text matched q (id, txn_date,
    direction, description, provider). Grouped by payee like ab-282, newest
    payee first; rows with no payee are left out. Returns (page, total)."""
    keys = extract_row_keys(rows, secret=secret)
    groups: dict[str, list[dict]] = {}
    for r in rows:
        key = keys[r["id"]]
        if key.kind is None:
            continue
        groups.setdefault(payee_group_key(key, r["direction"], r["id"]), []).append(r)

    items = []
    for group_key, members in groups.items():
        latest = max(members, key=lambda r: (r["txn_date"], str(r["id"])))
        key = keys[latest["id"]]
        count = len(members)
        items.append({
            "type": "payee",
            "id": group_key,
            "title": _display_name(key, None, latest["description"]),
            "subtitle": f"{count} transaction{'' if count == 1 else 's'} · last {_day(latest['txn_date'])}",
            # Transactions has no payee filter yet: the UI searches it with
            # this text. A person's name never becomes reusable text here.
            "link": {"group_key": group_key, "direction": latest["direction"],
                     "search": key.name if key.name and not key.is_person else q},
            "is_person": key.is_person,
            "row_count": count,
            "_last": latest["txn_date"],
        })
    items.sort(key=lambda i: i["id"])
    items.sort(key=lambda i: i["_last"], reverse=True)
    for i in items:
        del i["_last"]
    return items[offset:offset + limit], len(items)


def plan_item(r: dict) -> dict:
    return {
        "type": "plan",
        "id": str(r["id"]),
        "title": r["name"] or "Untitled plan",
        "subtitle": f"{r['period'].capitalize()} · {r['starts_at'].astimezone(_EAT):%b %Y} · {r['status'].capitalize()}",
        "link": {"plan_id": str(r["id"])},
    }


def account_item(r: dict) -> dict:
    parts = [r.get("provider") or _ACCOUNT_TYPES.get(r["account_type"], r["account_type"])]
    if r.get("provider"):
        parts.append(_ACCOUNT_TYPES.get(r["account_type"], r["account_type"]))
    if not r["is_active"]:
        parts.append("Inactive")
    return {
        "type": "account",
        "id": str(r["id"]),
        "title": r["nickname"],
        "subtitle": " · ".join(parts),
        "link": {"account_id": str(r["id"])},
    }
