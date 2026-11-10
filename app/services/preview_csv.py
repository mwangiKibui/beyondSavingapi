"""The statement-import preview as a CSV (ab-253): every row the preview
covers, its suggestion if it's still open, and what's already chosen if
it isn't - so the user can compare with their own records. Pure."""

import csv
import io
from uuid import UUID

CSV_COLUMNS = [
    "date",
    "description",
    "payee",
    "direction",
    "amount",
    "balance_after",
    "suggested_category",
    "is_new_category",
    "suggestion_source",
    "matched_merchant",
    "chosen_category",
]

# A cell starting with one of these runs as a formula in Excel/Sheets
# (CSV injection). Statement text goes straight into this file.
_FORMULA_START = ("=", "+", "-", "@", "\t", "\r")


def safe_cell(value: str) -> str:
    return "'" + value if value.startswith(_FORMULA_START) else value


def _money(value) -> str:
    return "" if value is None else f"{float(value):.2f}"


def _chosen(row: dict, category_names: dict[UUID, str]) -> str:
    if row["is_transfer"]:
        return "Transfer"
    if float(row["allocated"] or 0) == 0:
        return ""
    if row["category_count"] > 1 or (row["category_count"] == 1 and row["has_uncategorised"]):
        chosen = "Split"
    elif row["category_count"] == 1:
        chosen = category_names.get(row["category_id"], "")
    else:
        chosen = "No category"
    if float(row["allocated"]) < float(row["amount"]):
        chosen += " (partly)"
    return chosen


def build_preview_csv(
    rows: list[dict], *, preview_rows: list[dict], categories: list[dict], keys: dict | None = None
) -> str:
    """rows: every row the preview covers (repositories.get_import_rows);
    preview_rows: build_preview_rows' output for the open ones; keys: each
    row's RowKey, naming the payee on rows that are no longer open."""
    keys = keys or {}
    category_names = {c["id"]: c["name"] for c in categories}
    by_id = {p["transaction_id"]: p for p in preview_rows}

    buffer = io.StringIO()
    writer = csv.writer(buffer, lineterminator="\n")
    writer.writerow(CSV_COLUMNS)
    for row in rows:
        p = by_id.get(row["id"])
        s = (p or {}).get("suggestion") or {}
        key = keys.get(row["id"])
        payee = (p or {}).get("display_name") or (key and (key.name or key.display)) or ""
        values = [
            row["txn_date"].isoformat(),
            row["description"] or "",
            payee,
            row["direction"],
            _money(row["amount"]),
            _money(row.get("balance_after")),
            s.get("category_name") or "",
            ("yes" if s.get("is_new_category") else "no") if s else "",
            s.get("source") or "",
            s.get("merchant_name") or "",
            _chosen(row, category_names),
        ]
        writer.writerow([safe_cell(v) for v in values])
    return buffer.getvalue()
