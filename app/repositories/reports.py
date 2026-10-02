from dataclasses import dataclass
from datetime import date
from decimal import Decimal
from uuid import UUID

import asyncpg

ZERO = Decimal("0.00")


@dataclass(frozen=True)
class ReportFilters:
    """The one filter set shared across GET /reports, the CSV export, and
    the PDF export (ab-82/83/84/85/127) - built once per request by the
    router (see app/api/reports.py's _build_filters) and threaded through
    to fetch_report_rows, so all three endpoints are guaranteed to
    aggregate the exact same underlying rows instead of risking three
    independently-built WHERE clauses drifting apart.

    currency is never optional here - the router resolves the caller's
    own users.default_currency before constructing this, per
    docs/schema.sql's own comment on that column: reports are
    currency-FILTERED, never summed across currencies.
    """

    user_id: UUID
    currency: str
    account_id: UUID | None = None
    from_date: date | None = None
    to_date: date | None = None
    direction: str | None = None


async def resolve_report_currency(pool: asyncpg.Pool, *, user_id: UUID, currency: str | None) -> str:
    """currency defaults to the caller's own users.default_currency when
    the client doesn't pass one explicitly (docs/schema.sql's own comment
    on that column - the filter pre-selects this, it's never a conversion
    target)."""
    if currency is not None:
        return currency
    return await pool.fetchval("SELECT default_currency FROM users WHERE id = $1", user_id)


async def fetch_report_rows(pool: asyncpg.Pool, filters: ReportFilters) -> list[dict]:
    """One row per (transaction, allocation) pair matching the filters -
    LEFT JOIN so a transaction with zero allocations still appears once,
    with every allocation-side column NULL. This is the single query
    behind GET /reports, the CSV export, and the PDF export
    (ab-82/83/84/85/127); every aggregate those three produce is derived
    in Python (see build_report/build_csv_rows below) from this one
    result set, so they can never disagree with each other the way three
    independently-aggregated SQL queries risk doing.

    Mirrors budget_plans.py/notifications.py's own join pattern
    (allocations -> transactions -> accounts, scoped by user_id via
    accounts.user_id) and transactions.py's list_transactions
    ($N::type IS NULL OR ...) optional-filter convention for
    account_id/from/to/direction. currency scopes the TRANSACTION set to
    exactly the report's one currency - never a cross-currency sum.
    """
    rows = await pool.fetch(
        """
        SELECT
            t.id AS transaction_id,
            t.txn_date,
            t.amount AS txn_amount,
            t.currency AS txn_currency,
            t.direction,
            t.counterparty,
            t.description,
            acc.nickname AS account_nickname,
            a.id AS allocation_id,
            a.category_id,
            c.name AS category_name,
            c.type AS category_type,
            a.transfer_reason_id,
            a.amount AS alloc_amount,
            a.currency AS alloc_currency
        FROM transactions t
        JOIN accounts acc ON acc.id = t.account_id
        LEFT JOIN allocations a ON a.transaction_id = t.id
        LEFT JOIN categories c ON c.id = a.category_id
        WHERE acc.user_id = $1
          AND t.currency = $2
          AND ($3::uuid IS NULL OR t.account_id = $3)
          AND ($4::date IS NULL OR t.txn_date >= $4)
          AND ($5::date IS NULL OR t.txn_date <= $5)
          AND ($6::text IS NULL OR t.direction = $6)
        ORDER BY t.txn_date DESC, t.id, a.created_at
        """,
        filters.user_id,
        filters.currency,
        filters.account_id,
        filters.from_date,
        filters.to_date,
        filters.direction,
    )
    return [dict(row) for row in rows]


def _group_by_transaction(rows: list[dict]) -> dict[UUID, list[dict]]:
    """Groups fetch_report_rows' flat (transaction, allocation) rows back
    into one list per transaction, preserving the SQL's own
    txn_date DESC ordering (dicts preserve insertion order, and each
    transaction's rows are contiguous since the query orders by
    t.id after txn_date)."""
    grouped: dict[UUID, list[dict]] = {}
    for row in rows:
        grouped.setdefault(row["transaction_id"], []).append(row)
    return grouped


def build_report(rows: list[dict], *, currency: str) -> dict:
    """Pure-Python aggregation over fetch_report_rows' own result set -
    the shared data backing GET /reports (ab-82/83/127). Every figure
    below is derived from this exact set of rows, which is what keeps
    the JSON report, the CSV export, and the PDF export from ever
    disagreeing with each other.

    - total_in/total_out: sum of a transaction's own amount, per
      direction, EXCLUDING any transaction that has at least one
      transfer-shaped allocation (ab-79: "excludes transfer-tagged
      transactions" - the whole transaction is netted out, even the
      portion not covered by the transfer allocation).
    - by_category: sum of allocation amounts grouped by category_id,
      for allocations with a real category_id and no transfer_reason_id.
    - reconciled_no_category_total: sum of allocation amounts where both
      category_id and transfer_reason_id are NULL (ab-124's deliberate
      "reconciled, no category" state).
    - unreconciled_total: per transaction, its own amount minus the sum
      of its own allocations' amounts, floored at 0, summed across every
      matching transaction - a transfer-tagged transaction can still
      have an unreconciled remainder if only partially allocated, so
      this is computed independently of the transfer exclusion above.
    """
    total_in = ZERO
    total_out = ZERO
    unreconciled_total = ZERO
    reconciled_no_category_total = ZERO
    by_category: dict[UUID, dict] = {}

    for alloc_rows in _group_by_transaction(rows).values():
        first = alloc_rows[0]
        txn_amount = first["txn_amount"]
        direction = first["direction"]

        has_transfer = any(row["transfer_reason_id"] is not None for row in alloc_rows)
        allocated = sum(
            (row["alloc_amount"] for row in alloc_rows if row["allocation_id"] is not None),
            ZERO,
        )

        if not has_transfer:
            if direction == "in":
                total_in += txn_amount
            else:
                total_out += txn_amount

        remainder = txn_amount - allocated
        if remainder > ZERO:
            unreconciled_total += remainder

        for row in alloc_rows:
            if row["allocation_id"] is None or row["transfer_reason_id"] is not None:
                continue
            if row["category_id"] is None:
                reconciled_no_category_total += row["alloc_amount"]
                continue
            bucket = by_category.setdefault(
                row["category_id"],
                {
                    "category_id": row["category_id"],
                    "category_name": row["category_name"],
                    "category_type": row["category_type"],
                    "total": ZERO,
                },
            )
            bucket["total"] += row["alloc_amount"]

    return {
        "currency": currency,
        "total_in": total_in,
        "total_out": total_out,
        "net": total_in - total_out,
        "by_category": sorted(by_category.values(), key=lambda b: b["category_name"] or ""),
        "reconciled_no_category_total": reconciled_no_category_total,
        "unreconciled_total": unreconciled_total,
    }


async def get_report(pool: asyncpg.Pool, filters: ReportFilters) -> dict:
    """GET /reports' own data (ab-82/83/127) - fetches the shared row set
    and aggregates it in one pass."""
    rows = await fetch_report_rows(pool, filters)
    return build_report(rows, currency=filters.currency)


def build_csv_rows(rows: list[dict]) -> list[dict]:
    """One row per TRANSACTION (not allocation) for the CSV export
    (ab-84), built from the exact same rows GET /reports aggregates -
    a transaction with several category splits comma-joins their names;
    one with only a transfer-shaped split or no allocation at all shows
    an em dash in the category column.
    """
    csv_rows = []
    for alloc_rows in _group_by_transaction(rows).values():
        first = alloc_rows[0]
        category_names = [
            row["category_name"]
            for row in alloc_rows
            if row["category_id"] is not None and row["transfer_reason_id"] is None
        ]
        csv_rows.append(
            {
                "txn_date": first["txn_date"],
                "account_nickname": first["account_nickname"],
                "direction": first["direction"],
                "amount": first["txn_amount"],
                "currency": first["txn_currency"],
                "categories": ", ".join(category_names) if category_names else "—",
                "counterparty": first["counterparty"] or "",
                "description": first["description"] or "",
            }
        )
    return csv_rows


async def get_report_csv_rows(pool: asyncpg.Pool, filters: ReportFilters) -> list[dict]:
    """The CSV export's own data (ab-84) - same underlying rows as
    GET /reports, shaped per-transaction instead of aggregated."""
    rows = await fetch_report_rows(pool, filters)
    return build_csv_rows(rows)
