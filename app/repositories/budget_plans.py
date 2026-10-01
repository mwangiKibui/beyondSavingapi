from datetime import datetime
from decimal import Decimal
from uuid import UUID

import asyncpg

from app.services.budget_state import DEFAULT_NEAR_THRESHOLD, derive_budget_state

# MVP1 is single-currency (developer decision, 2026-09-30) - every plan is
# created in this currency, no currency field is accepted from the client.
# budget_plans.currency's own column default is this same value, so leaving
# it out of every INSERT here is enough; this constant exists only so a
# caller that needs to *display* it (there is none yet) has one source of
# truth instead of a second hardcoded 'KES'.
DEFAULT_CURRENCY = "KES"


class DuplicateBudget(Exception):
    """Raised when a plan already has a budget for that category (UNIQUE
    (plan_id, category_id))."""


class OverlappingBudgetPlan(Exception):
    """Raised when the user already has a plan of the SAME period whose
    window overlaps the one being created (ab-147) - carries the
    conflicting plan's row so the API layer can name it in the error."""

    def __init__(self, conflicting_plan: dict):
        self.conflicting_plan = conflicting_plan
        super().__init__("Overlapping budget plan")


async def create_budget_plan(
    pool: asyncpg.Pool,
    *,
    user_id: UUID,
    name: str | None,
    period: str,
    starts_at: datetime,
    ends_at: datetime,
    total_cap: Decimal | None,
    is_recurring: bool,
) -> dict:
    # Only one plan per PERIOD TYPE may cover any given date range - a
    # daily, weekly, monthly, and annual plan can all run at once (they're
    # different granularities, meant to coexist), but two monthly plans
    # can't overlap. Overlap is checked regardless of whether the existing
    # plan is "active today" - a future month that hasn't started yet
    # still conflicts with another plan already covering that range.
    # Inclusive calendar-date comparison, matching how list_budget_plans
    # already scopes a plan's own consumption (txn_date BETWEEN
    # starts_at::date AND ends_at::date).
    conflict = await pool.fetchrow(
        """
        SELECT id, name, starts_at, ends_at
        FROM budget_plans
        WHERE user_id = $1
          AND period = $2
          AND starts_at::date <= $4::date
          AND $3::date <= ends_at::date
        LIMIT 1
        """,
        user_id,
        period,
        starts_at,
        ends_at,
    )
    if conflict is not None:
        raise OverlappingBudgetPlan(dict(conflict))

    row = await pool.fetchrow(
        """
        INSERT INTO budget_plans (user_id, name, period, starts_at, ends_at, total_cap, is_recurring)
        VALUES ($1, $2, $3, $4, $5, $6, $7)
        RETURNING id, name, period, starts_at, ends_at, total_cap, currency, is_recurring, is_active, created_at
        """,
        user_id,
        name,
        period,
        starts_at,
        ends_at,
        total_cap,
        is_recurring,
    )
    return dict(row)


async def get_budget_plan(pool: asyncpg.Pool, *, plan_id: UUID, user_id: UUID) -> dict | None:
    row = await pool.fetchrow(
        """
        SELECT id, name, period, starts_at, ends_at, total_cap, currency, is_recurring, is_active, created_at
        FROM budget_plans
        WHERE id = $1 AND user_id = $2
        """,
        plan_id,
        user_id,
    )
    return dict(row) if row else None


async def add_budget(
    pool: asyncpg.Pool,
    *,
    user_id: UUID,
    plan_id: UUID,
    category_id: UUID,
    limit_amount: Decimal | None,
) -> dict | None:
    """Returns None if the plan doesn't exist or isn't owned by user_id.
    category_id's own existence/ownership is the caller's job to check
    first (mirrors create-allocations' own category-lookup convention) -
    this only guards the plan side, then lets the FK/unique constraints
    catch a bad or duplicate category_id.
    """
    plan = await get_budget_plan(pool, plan_id=plan_id, user_id=user_id)
    if plan is None:
        return None

    try:
        row = await pool.fetchrow(
            """
            INSERT INTO budgets (plan_id, category_id, limit_amount)
            VALUES ($1, $2, $3)
            RETURNING id, plan_id, category_id, limit_amount, created_at
            """,
            plan_id,
            category_id,
            limit_amount,
        )
    except asyncpg.UniqueViolationError as exc:
        raise DuplicateBudget() from exc
    return dict(row)


async def update_budget(
    pool: asyncpg.Pool,
    *,
    user_id: UUID,
    budget_id: UUID,
    limit_amount: Decimal | None,
) -> dict | None:
    row = await pool.fetchrow(
        """
        UPDATE budgets b
        SET limit_amount = $1, updated_at = now()
        FROM budget_plans p
        WHERE b.id = $2 AND b.plan_id = p.id AND p.user_id = $3
        RETURNING b.id, b.plan_id, b.category_id, b.limit_amount, b.created_at
        """,
        limit_amount,
        budget_id,
        user_id,
    )
    return dict(row) if row else None


async def list_plan_money_flows(
    pool: asyncpg.Pool,
    *,
    user_id: UUID,
    plan_id: UUID,
) -> dict | None:
    """The reconciled allocations behind a plan's Money In / Money Out tabs
    (ab-144/145) - the exact rows that would sum to total_income/
    total_expenditure (ab-142), broken out per transaction so the user can
    see what actually makes up each total. A transfer-shaped allocation
    (ab-132 - money moving between the user's own buckets) is excluded,
    same as those totals. Returns None if the plan doesn't exist or isn't
    owned by user_id.
    """
    plan = await get_budget_plan(pool, plan_id=plan_id, user_id=user_id)
    if plan is None:
        return None

    async def fetch_direction(direction: str) -> list[dict]:
        rows = await pool.fetch(
            """
            SELECT
                a.id,
                t.txn_date,
                t.counterparty,
                t.description,
                c.name AS category_name,
                a.amount,
                a.currency
            FROM allocations a
            JOIN transactions t ON t.id = a.transaction_id
            LEFT JOIN categories c ON c.id = a.category_id
            WHERE a.currency = $1
              AND t.direction = $2
              AND a.transfer_reason_id IS NULL
              AND t.txn_date >= $3::date
              AND t.txn_date <= $4::date
            ORDER BY t.txn_date DESC
            """,
            plan["currency"],
            direction,
            plan["starts_at"],
            plan["ends_at"],
        )
        return [dict(row) for row in rows]

    return {
        "money_in": await fetch_direction("in"),
        "money_out": await fetch_direction("out"),
    }


async def list_budget_plans(
    pool: asyncpg.Pool,
    *,
    user_id: UUID,
) -> list[dict]:
    """Every one of the user's plans, most recently started first, each
    with its budgets and their derived consumption. Consumption is summed
    from allocations by CATEGORY + DATE WINDOW (docs/schema.sql's own
    "remaining is derived" convention) - not via allocations.budget_id,
    which is a separate, not-yet-built linkage for the reconciliation-time
    "plans you're affecting" panel (ab-55/56/62). Consumption is also
    scoped to the plan's OWN currency: harmless under MVP1's single
    currency, and correct if a second-currency account ever exists before
    the currency-matching/conversion flow (ab-56/ab-62) is built.

    The near/at/over threshold is each user's own `users.near_threshold`
    (ab-91/94's preferences setting - defaults to 0.80, not yet
    user-editable since that ticket hasn't shipped) rather than a hardcoded
    constant, so this already respects the setting once it is.
    """
    near_threshold = await pool.fetchval(
        "SELECT near_threshold FROM users WHERE id = $1", user_id
    ) or DEFAULT_NEAR_THRESHOLD

    plan_rows = await pool.fetch(
        """
        SELECT id, name, period, starts_at, ends_at, total_cap, currency, is_recurring, is_active, created_at
        FROM budget_plans
        WHERE user_id = $1
        ORDER BY starts_at DESC
        """,
        user_id,
    )

    plans = []
    for plan_row in plan_rows:
        plan = dict(plan_row)
        budget_rows = await pool.fetch(
            """
            SELECT
                b.id,
                b.category_id,
                c.name AS category_name,
                c.type AS category_type,
                b.limit_amount,
                COALESCE((
                    SELECT SUM(a.amount)
                    FROM allocations a
                    JOIN transactions t ON t.id = a.transaction_id
                    WHERE a.category_id = b.category_id
                      AND a.currency = $2
                      AND t.txn_date >= $3::date
                      AND t.txn_date <= $4::date
                ), 0) AS consumed
            FROM budgets b
            JOIN categories c ON c.id = b.category_id
            WHERE b.plan_id = $1
            ORDER BY c.name ASC
            """,
            plan["id"],
            plan["currency"],
            plan["starts_at"],
            plan["ends_at"],
        )

        budgets = []
        for budget_row in budget_rows:
            budget = dict(budget_row)
            derived = derive_budget_state(
                category_type=budget["category_type"],
                limit_amount=budget["limit_amount"],
                consumed=budget["consumed"],
                near_threshold=near_threshold,
            )
            budgets.append({**budget, **derived})

        # ab-142: total_expenditure/total_income are whole-plan sums across
        # EVERY real allocation in the window (not a sum of the category
        # budgets), always computed regardless of whether total_cap is
        # set - they power the Budget plans listing's own columns, not
        # the cap progress bar. A transfer-shaped allocation (ab-132 -
        # money moving between the user's own buckets, e.g. a loan
        # repayment) is explicitly NOT real income/expense, so it's
        # excluded here the same way the per-category `consumed` subquery
        # above only ever matches a real category_id.
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

        # The overall total_cap (if set) measures against that same
        # total_expenditure (docs/schema.sql: "on top of the per-category
        # budgets") - reused here rather than summed again.
        total_consumed = None
        total_derived = None
        if plan["total_cap"] is not None:
            total_consumed = total_expenditure
            total_derived = derive_budget_state(
                category_type="expense",
                limit_amount=plan["total_cap"],
                consumed=total_consumed,
                near_threshold=near_threshold,
            )

        plans.append({
            **plan,
            "budgets": budgets,
            "total_consumed": total_consumed,
            "total_state": total_derived["state"] if total_derived else None,
            "total_expenditure": total_expenditure,
            "total_income": total_income,
        })

    return plans
