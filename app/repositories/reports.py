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


async def get_category_summary(pool: asyncpg.Pool, filters: ReportFilters, *, category_type: str) -> list[dict]:
    """Category Summary export's own data (ab-152, GET
    /reports/categories/export.csv|.pdf) - reuses build_report's own
    by-category aggregation (the exact same `by_category` figures GET
    /reports already serves) and just filters it down to one
    category_type ("expense" or "income"), rather than re-deriving the
    category breakdown from scratch. filters.account_id/direction are
    expected to be None here (the export only takes category_type/
    from/to) - threading the same ReportFilters dataclass through keeps
    this on the exact same fetch-then-aggregate pipeline as every other
    /reports endpoint.
    """
    rows = await fetch_report_rows(pool, filters)
    report = build_report(rows, currency=filters.currency)
    return [item for item in report["by_category"] if item["category_type"] == category_type]


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


async def fetch_account_summary_rows(pool: asyncpg.Pool, *, user_id: UUID, account_id: UUID | None) -> list[dict]:
    """One row per (account, transaction) pair backing Account Summary
    (ab-150, GET /reports/accounts) - LEFT JOIN so an account with zero
    transactions still appears once, with every transaction-side column
    NULL. **All-time, no date filter at all** - unlike fetch_report_rows
    above, this report is explicitly scoped to "every account we have,"
    not a date window. has_transfer_allocation is computed per-transaction
    here (not filtered out in SQL) so build_account_summary below can
    apply the same "exclude transfers from real income/expense"
    exclusion build_report applies, in pure Python, the same
    fetch-then-aggregate split as fetch_report_rows/build_report.
    """
    rows = await pool.fetch(
        """
        SELECT
            a.id AS account_id,
            a.nickname AS account_nickname,
            t.id AS transaction_id,
            t.amount AS txn_amount,
            t.direction,
            EXISTS (
                SELECT 1 FROM allocations al
                WHERE al.transaction_id = t.id AND al.transfer_reason_id IS NOT NULL
            ) AS has_transfer_allocation
        FROM accounts a
        LEFT JOIN transactions t ON t.account_id = a.id
        WHERE a.user_id = $1
          AND ($2::uuid IS NULL OR a.id = $2)
        ORDER BY a.nickname
        """,
        user_id,
        account_id,
    )
    return [dict(row) for row in rows]


def build_account_summary(rows: list[dict]) -> list[dict]:
    """Pure-Python aggregation over fetch_account_summary_rows' own result
    set (ab-150) - mirrors build_report's own fetch-then-aggregate split
    above. money_in/money_out are each account's own transactions summed
    by direction, EXCLUDING any transaction with a transfer-shaped
    allocation (has_transfer_allocation) - same "exclude transfers from
    real income/expense" convention as build_report's total_in/total_out
    and budget_plans.py's own totals. An account has exactly one
    currency, so there's no cross-currency rollup to do here, unlike
    build_report.
    """
    summary: dict[UUID, dict] = {}
    for row in rows:
        bucket = summary.setdefault(
            row["account_id"],
            {
                "account_id": row["account_id"],
                "account_nickname": row["account_nickname"],
                "money_in": ZERO,
                "money_out": ZERO,
            },
        )
        if row["transaction_id"] is None or row["has_transfer_allocation"]:
            continue
        if row["direction"] == "in":
            bucket["money_in"] += row["txn_amount"]
        else:
            bucket["money_out"] += row["txn_amount"]

    return sorted(summary.values(), key=lambda b: b["account_nickname"])


async def get_account_summary(pool: asyncpg.Pool, *, user_id: UUID, account_id: UUID | None) -> list[dict]:
    """Account Summary's own data (ab-150) - fetches the shared row set
    and aggregates it in one pass, same convention as get_report above."""
    rows = await fetch_account_summary_rows(pool, user_id=user_id, account_id=account_id)
    return build_account_summary(rows)


async def fetch_budget_plan_rows(pool: asyncpg.Pool, *, user_id: UUID) -> list[dict]:
    """Every one of the user's budget plans (ab-152, GET
    /reports/budget-plans/export.csv|.pdf) - unfiltered by window, same
    "fetch everything, then filter/aggregate in pure Python" split as
    fetch_report_rows/build_report and fetch_account_summary_rows/
    build_account_summary above. Keeping the window-overlap filter
    (filter_budget_plans_by_window below) in pure Python rather than the
    WHERE clause is what makes it directly unit-testable against
    hand-built plan dicts, the same way build_report's own filtering
    logic is.
    """
    rows = await pool.fetch(
        """
        SELECT id, name, starts_at, ends_at, total_cap, currency
        FROM budget_plans
        WHERE user_id = $1
        ORDER BY starts_at DESC
        """,
        user_id,
    )
    return [dict(row) for row in rows]


def filter_budget_plans_by_window(
    plans: list[dict], *, from_date: date | None, to_date: date | None
) -> list[dict]:
    """The Budget Plans export's own window-overlap filter (ab-152) - a
    plan is kept when its window OVERLAPS [from_date, to_date], not just
    when it STARTS inside that range (ab-151's frontend already expects
    this overlap semantic for the same report; this is the backend doing
    the same filtering itself rather than the frontend fetching
    everything unfiltered). Both bounds are optional - omitting one
    leaves that side unbounded, same optional-filter convention as every
    other /reports filter.

    starts_at::date <= to_date AND ends_at::date >= from_date - the
    standard two-interval-overlap test, so a plan that starts BEFORE
    from_date but ends INSIDE the range is still kept (its ends_at is >=
    from_date and its starts_at is always <= to_date in that case), same
    as a plan that starts inside the range but ends after to_date, or
    one entirely inside it.
    """
    return [
        plan
        for plan in plans
        if (to_date is None or plan["starts_at"].date() <= to_date)
        and (from_date is None or plan["ends_at"].date() >= from_date)
    ]


async def get_budget_plans_report(
    pool: asyncpg.Pool, *, user_id: UUID, from_date: date | None, to_date: date | None
) -> list[dict]:
    """Budget Plans export's own data (ab-152) - fetches every plan,
    keeps only the ones whose window overlaps from/to (see
    filter_budget_plans_by_window), then computes each kept plan's
    money_in/money_out the same way list_budget_plans' total_income/
    total_expenditure already are (app/repositories/budget_plans.py):
    summed from real (non-transfer) allocations in the PLAN'S OWN
    currency and window (plan["starts_at"]/plan["ends_at"]), not the
    query's from/to range - a plan's totals are always its own full
    window's activity, the same way the Budget Plans listing's totals
    are, regardless of which from/to the caller used to select which
    plans to include in this export.
    """
    plans = await fetch_budget_plan_rows(pool, user_id=user_id)
    plans = filter_budget_plans_by_window(plans, from_date=from_date, to_date=to_date)

    result = []
    for plan in plans:
        total_expenditure = await pool.fetchval(
            """
            SELECT COALESCE(SUM(a.amount), 0)
            FROM allocations a
            JOIN transactions t ON t.id = a.transaction_id
            WHERE a.currency = $1
              AND t.direction = 'out'
              AND a.transfer_reason_id IS NULL
              AND t.txn_date >= $2::date
              AND t.txn_date <= $3::date
            """,
            plan["currency"],
            plan["starts_at"],
            plan["ends_at"],
        )
        total_income = await pool.fetchval(
            """
            SELECT COALESCE(SUM(a.amount), 0)
            FROM allocations a
            JOIN transactions t ON t.id = a.transaction_id
            WHERE a.currency = $1
              AND t.direction = 'in'
              AND a.transfer_reason_id IS NULL
              AND t.txn_date >= $2::date
              AND t.txn_date <= $3::date
            """,
            plan["currency"],
            plan["starts_at"],
            plan["ends_at"],
        )
        result.append({**plan, "money_in": total_income, "money_out": total_expenditure})

    return result


def _resolve_transfer_source_label(row: dict) -> str:
    """Resolves a transfer-shaped allocation's source to a display label
    (ab-150) - exactly one of source_account_id, source_sub_ledger_id, or
    source_description is ever set (the mutual exclusivity Create-
    allocations/ab-134 enforces on write), checked in that priority
    order. The "-" fallback shouldn't normally happen (a transfer-shaped
    allocation always has at least a description), but is kept for
    defense in depth rather than letting a None through to the response.
    """
    if row["source_account_id"] is not None:
        return row["source_account_nickname"]
    if row["source_sub_ledger_id"] is not None:
        return f"{row['source_sub_ledger_account_nickname']} / {row['source_sub_ledger_name']}"
    if row["source_description"]:
        return row["source_description"]
    return "—"


async def get_transfers(
    pool: asyncpg.Pool,
    *,
    user_id: UUID,
    account_id: UUID | None,
    from_date: date | None,
    to_date: date | None,
) -> list[dict]:
    """Account-to-Account Transfer list (ab-150, GET /reports/transfers) -
    one row per transfer-shaped allocation (transfer_reason_id IS NOT
    NULL), newest first. Scoped to the caller's own accounts via the
    destination account's own user_id (dest_acc.user_id = $1) - same
    "join through accounts to enforce ownership" convention as every
    other endpoint in this codebase; a transfer's source side (when it's
    a tracked account/sub-ledger rather than a free-text description) is
    already guaranteed to belong to the same user by Create-allocations'
    own write-time validation (ab-134), so it needs no separate scoping
    here.

    account_id (optional) matches a transfer where this account is
    EITHER the source (source_account_id directly, or via
    source_sub_ledger_id's parent account - ssl.account_id) OR the
    destination (t.account_id, the account the allocation's own
    transaction landed on). from/to filter transactions.txn_date, same
    optional-filter convention as fetch_report_rows above.

    amount/currency are the allocation's own amount/currency columns -
    not original_amount/original_currency - mirroring fetch_report_rows'
    own alloc_amount/alloc_currency choice above; the two pairs are
    identical today since insert_allocations sets amount/currency equal
    to original_amount/original_currency (no FX conversion exists yet),
    but this keeps both reports reading from the same pair should that
    change.
    """
    rows = await pool.fetch(
        """
        SELECT
            al.id,
            tr.name AS transfer_reason_name,
            al.source_account_id,
            sa.nickname AS source_account_nickname,
            al.source_sub_ledger_id,
            ssl.name AS source_sub_ledger_name,
            ssl_acc.nickname AS source_sub_ledger_account_nickname,
            al.source_description,
            dest_acc.nickname AS destination_account_name,
            al.amount,
            al.currency,
            t.txn_date AS date
        FROM allocations al
        JOIN transactions t ON t.id = al.transaction_id
        JOIN accounts dest_acc ON dest_acc.id = t.account_id
        LEFT JOIN transfer_reasons tr ON tr.id = al.transfer_reason_id
        LEFT JOIN accounts sa ON sa.id = al.source_account_id
        LEFT JOIN sub_ledgers ssl ON ssl.id = al.source_sub_ledger_id
        LEFT JOIN accounts ssl_acc ON ssl_acc.id = ssl.account_id
        WHERE dest_acc.user_id = $1
          AND al.transfer_reason_id IS NOT NULL
          AND (
              $2::uuid IS NULL
              OR al.source_account_id = $2
              OR ssl.account_id = $2
              OR t.account_id = $2
          )
          AND ($3::date IS NULL OR t.txn_date >= $3)
          AND ($4::date IS NULL OR t.txn_date <= $4)
        ORDER BY t.txn_date DESC, al.created_at DESC
        """,
        user_id,
        account_id,
        from_date,
        to_date,
    )
    return [
        {
            "id": row["id"],
            "transfer_reason_name": row["transfer_reason_name"],
            "source_label": _resolve_transfer_source_label(row),
            "destination_account_name": row["destination_account_name"],
            "amount": row["amount"],
            "currency": row["currency"],
            "date": row["date"],
        }
        for row in (dict(row) for row in rows)
    ]
