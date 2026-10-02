from datetime import date
from decimal import Decimal
from uuid import UUID

import asyncpg

from app.repositories.reports import (
    ZERO,
    fetch_budget_plan_rows,
    filter_budget_plans_by_window,
    resolve_report_currency,
)

__all__ = ["get_dashboard", "resolve_dashboard_currency"]

# resolve_report_currency's own "default to the user's own
# default_currency" behavior applies identically here - re-exported under
# a dashboard-specific name so callers don't need to know this reuses
# reports.py internally.
resolve_dashboard_currency = resolve_report_currency


async def fetch_dashboard_rows(
    pool: asyncpg.Pool, *, user_id: UUID, currency: str, from_date: date, to_date: date
) -> list[dict]:
    """One row per (account, transaction) for every one of the user's
    accounts IN THIS CURRENCY, within [from_date, to_date] - backs both
    the dashboard's overall total_in/total_out and its per-account money-
    in/money-out breakdown from the exact same row set, so the two always
    agree with each other (same "a transaction with any transfer-shaped
    allocation is excluded entirely" convention as build_report and the
    original fetch_account_summary_rows).

    LEFT JOIN (not JOIN), with the date filter folded into the JOIN
    condition rather than a WHERE clause, so an account with zero
    transactions in this window still appears once (money_in/money_out
    both zero) instead of silently dropping out of the account-
    performance breakdown - every account currently showing $0 activity
    this period is itself a fact worth surfacing, not noise to hide.
    An account has exactly one currency (same assumption
    build_account_summary's own docstring makes), so filtering accounts
    by currency already scopes every joined transaction to it too.
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
        LEFT JOIN transactions t
            ON t.account_id = a.id AND t.txn_date >= $3 AND t.txn_date <= $4
        WHERE a.user_id = $1 AND a.currency = $2
        ORDER BY a.nickname
        """,
        user_id,
        currency,
        from_date,
        to_date,
    )
    return [dict(row) for row in rows]


def build_dashboard(rows: list[dict], *, total_budget: Decimal, currency: str) -> dict:
    """Pure-Python aggregation over fetch_dashboard_rows' own result set -
    mirrors build_report/build_account_summary's own fetch-then-aggregate
    split. total_in/total_out (and each account's own money_in/money_out)
    EXCLUDE any transaction with a transfer-shaped allocation, the same
    "transfers aren't real income/expense" convention every other report
    in this codebase uses - deliberately the RAW transaction amount, not
    the reconciled/allocated amount Account Summary now uses (2026-10-02),
    so that summing every account's own money_in here always reproduces
    the overall total_in exactly; reconciliation status is a bookkeeping
    concern this dashboard isn't scoped to.
    """
    total_in = ZERO
    total_out = ZERO
    accounts: dict[UUID, dict] = {}

    for row in rows:
        bucket = accounts.setdefault(
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
            total_in += row["txn_amount"]
        else:
            bucket["money_out"] += row["txn_amount"]
            total_out += row["txn_amount"]

    return {
        "currency": currency,
        "total_budget": total_budget,
        "total_in": total_in,
        "total_out": total_out,
        "net": total_in - total_out,
        "accounts": sorted(accounts.values(), key=lambda b: b["account_nickname"]),
    }


async def get_dashboard(
    pool: asyncpg.Pool, *, user_id: UUID, currency: str, from_date: date, to_date: date
) -> dict:
    """The Dashboard page's own data (2026-10-03) - total_budget is the
    sum of every budget plan's total_cap whose own window OVERLAPS
    [from_date, to_date] (same overlap semantic the Budget Plans report
    already uses via filter_budget_plans_by_window), scoped to this one
    currency and skipping any plan with no cap set at all. total_in/
    total_out/accounts come from fetch_dashboard_rows/build_dashboard
    above.
    """
    plans = await fetch_budget_plan_rows(pool, user_id=user_id)
    overlapping_plans = filter_budget_plans_by_window(plans, from_date=from_date, to_date=to_date)
    total_budget = sum(
        (plan["total_cap"] for plan in overlapping_plans if plan["currency"] == currency and plan["total_cap"] is not None),
        ZERO,
    )

    rows = await fetch_dashboard_rows(pool, user_id=user_id, currency=currency, from_date=from_date, to_date=to_date)
    return build_dashboard(rows, total_budget=total_budget, currency=currency)
