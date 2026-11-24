"""ab-309: plans, their limits (migration 000035) and the usage counts the
limits are checked against. Every query takes a pool or a connection."""

from datetime import datetime
from uuid import UUID


async def get_plan_limit(conn, *, user_id: UUID, key: str) -> int | None:
    """The user's plan's value for a limit. None = unlimited: a NULL value,
    or no row for that key on the plan."""
    return await conn.fetchval(
        """
        SELECT pl.value
        FROM users u
        JOIN plan_limits pl ON pl.plan_id = u.plan_id AND pl.limit_key = $2
        WHERE u.id = $1
        """,
        user_id,
        key,
    )


async def count_imports_reached_allocation(conn, *, user_id: UUID, start: datetime, end: datetime) -> int:
    """Imports uploaded in [start, end) that reached the Allocation step.

    "Reached Allocation" in data = status 'parsed' AND the upload added at
    least one row (a transaction whose import_id is this import). That
    leaves out: pending (not read yet), failed (and awaiting_support once
    it exists - any status other than parsed), and "Already uploaded"
    (parsed, but every row was already recorded by an earlier upload, so it
    added 0 - same rule as the list's added_count, ab-297)."""
    return await conn.fetchval(
        """
        SELECT COUNT(*)
        FROM statement_imports si
        JOIN accounts a ON a.id = si.account_id
        WHERE a.user_id = $1
          AND si.created_at >= $2 AND si.created_at < $3
          AND si.status = 'parsed'
          AND EXISTS (SELECT 1 FROM transactions t WHERE t.import_id = si.id)
        """,
        user_id,
        start,
        end,
    )


async def count_active_accounts(conn, *, user_id: UUID) -> int:
    """Deactivated accounts don't count towards accounts_max."""
    return await conn.fetchval(
        "SELECT COUNT(*) FROM accounts WHERE user_id = $1 AND is_active = true",
        user_id,
    )


async def list_plans_with_limits(conn) -> list[dict]:
    rows = await conn.fetch(
        """
        SELECT p.id, p.key, p.name, pl.limit_key, pl.value
        FROM plans p
        LEFT JOIN plan_limits pl ON pl.plan_id = p.id
        ORDER BY p.created_at, p.key, pl.limit_key
        """
    )
    plans: dict[UUID, dict] = {}
    for r in rows:
        plan = plans.setdefault(r["id"], {"id": r["id"], "key": r["key"], "name": r["name"], "limits": []})
        if r["limit_key"] is not None:
            plan["limits"].append({"limit_key": r["limit_key"], "value": r["value"]})
    return list(plans.values())


async def plan_exists(conn, *, plan_id: UUID) -> bool:
    return await conn.fetchval("SELECT EXISTS (SELECT 1 FROM plans WHERE id = $1)", plan_id)


async def update_plan_limit(conn, *, plan_id: UUID, limit_key: str, value: int | None) -> dict | None:
    """Only an existing row is updated - None when the plan has no such
    limit."""
    row = await conn.fetchrow(
        """
        UPDATE plan_limits SET value = $3, updated_at = now()
        WHERE plan_id = $1 AND limit_key = $2
        RETURNING limit_key, value
        """,
        plan_id,
        limit_key,
        value,
    )
    return dict(row) if row else None
