from datetime import datetime
from uuid import UUID

import asyncpg

from app.services.money_plan_summary import summarise_money_plan

# Columns every money-plan endpoint returns. status and currency are left to
# their column defaults ('draft' and 'KES') on insert - MVP1 is
# single-currency, the same as budget_plans.
_PLAN_COLUMNS = "id, name, period, starts_at, ends_at, currency, status, converted_at, created_at, updated_at"


async def create_money_plan(
    pool: asyncpg.Pool,
    *,
    user_id: UUID,
    name: str | None,
    period: str,
    starts_at: datetime,
    ends_at: datetime,
) -> dict:
    # Unlike budget_plans, overlapping windows are allowed: a money plan is a
    # sketch, and drafting two versions of the same month side by side is
    # expected. The one-plan-per-period rule only bites when a plan is
    # converted into a budget plan (ab-238).
    row = await pool.fetchrow(
        f"""
        INSERT INTO money_plans (user_id, name, period, starts_at, ends_at)
        VALUES ($1, $2, $3, $4, $5)
        RETURNING {_PLAN_COLUMNS}
        """,
        user_id,
        name,
        period,
        starts_at,
        ends_at,
    )
    return dict(row)


async def list_money_plans(pool: asyncpg.Pool, *, user_id: UUID, status: str | None) -> list[dict]:
    # One row per plan with its line totals split by type. LEFT JOIN so a
    # plan with no lines yet still appears (all totals 0). Income lines are
    # always 'general' (money_plan_entries_income_is_general), so kind alone
    # identifies them; expenses split three ways on entry_type.
    rows = await pool.fetch(
        """
        SELECT p.id, p.name, p.period, p.starts_at, p.ends_at, p.currency, p.status,
               p.converted_at, p.created_at, p.updated_at,
               COUNT(e.id) AS entry_count,
               COALESCE(SUM(e.amount) FILTER (WHERE e.kind = 'income'), 0.00) AS income,
               COALESCE(SUM(e.amount) FILTER (WHERE e.kind = 'expense' AND e.entry_type = 'general'), 0.00)
                   AS spending,
               COALESCE(SUM(e.amount) FILTER (WHERE e.entry_type = 'loan_repayment'), 0.00) AS loan_repayments,
               COALESCE(SUM(e.amount) FILTER (WHERE e.entry_type = 'saving'), 0.00) AS savings
        FROM money_plans p
        LEFT JOIN money_plan_entries e ON e.plan_id = p.id
        WHERE p.user_id = $1
          AND ($2::text IS NULL OR p.status = $2)
        GROUP BY p.id
        ORDER BY p.starts_at DESC, p.created_at DESC
        """,
        user_id,
        status,
    )

    plans = []
    for row in rows:
        plan = dict(row)
        plan["summary"] = summarise_money_plan(
            income=plan.pop("income"),
            spending=plan.pop("spending"),
            loan_repayments=plan.pop("loan_repayments"),
            savings=plan.pop("savings"),
        )
        plans.append(plan)
    return plans
