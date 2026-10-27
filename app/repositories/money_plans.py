from datetime import datetime
from uuid import UUID

import asyncpg

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
