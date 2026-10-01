import logging
from datetime import date
from decimal import Decimal
from uuid import UUID

import asyncpg

from app.services.budget_state import DEFAULT_NEAR_THRESHOLD, derive_budget_state

logger = logging.getLogger(__name__)

# Matches budget_state.py's own expense progression (ok -> near -> at ->
# over) - a plain rank so "did this allocation push the state UP" is a
# simple integer comparison. Income's states ("on_track"/"good") never
# appear here since generate_budget_alerts skips income categories before
# this is ever consulted.
_STATE_RANK = {"ok": 0, "near": 1, "at": 2, "over": 3}

_ALERT_TITLES = {
    "near": "Budget near limit",
    "at": "Budget at limit",
    "over": "Budget over limit",
}


def _alert_copy(state: str, category_name: str, plan_name: str | None) -> tuple[str, str]:
    plan_label = plan_name or "your budget plan"
    phrasing = {
        "near": f'"{category_name}" is approaching its limit in {plan_label}.',
        "at": f'"{category_name}" has reached its limit in {plan_label}.',
        "over": f'"{category_name}" has gone over its limit in {plan_label}.',
    }
    return _ALERT_TITLES[state], phrasing[state]


async def generate_budget_alerts(
    pool: asyncpg.Pool,
    *,
    user_id: UUID,
    category_id: UUID,
    txn_date: date,
    currency: str,
    new_amount: Decimal,
) -> None:
    """Called once per distinct (category_id, txn_date) pair among a batch
    of just-INSERTed allocations (ab-77), after the insert has already
    committed. For every budget (across this user's budget_plans) whose
    category matches and whose window covers txn_date, recomputes the
    live `consumed` sum (same query as list_budget_plans.py) as
    consumed_after, derives consumed_before by subtracting new_amount
    (the just-inserted total for this exact group - there's no earlier
    snapshot to query once the insert has happened), and raises exactly
    one budget_alert notification per budget whose derived state crosses
    UP into near/at/over as a result.

    Income categories have no near/at/over scale (derive_budget_state
    only ever returns on_track/good for them) so they're skipped up
    front - state only ever worsens for an expense category's allocation,
    since allocations are append-only (consumed never decreases here).
    Caller is expected to wrap this in a try/except - a failure here must
    never break the reconciliation response that triggered it.
    """
    category = await pool.fetchrow(
        "SELECT type FROM categories WHERE id = $1 AND user_id = $2", category_id, user_id
    )
    if category is None or category["type"] != "expense":
        return

    near_threshold = (
        await pool.fetchval("SELECT near_threshold FROM users WHERE id = $1", user_id)
    ) or DEFAULT_NEAR_THRESHOLD

    budgets = await pool.fetch(
        """
        SELECT b.id AS budget_id, b.plan_id, b.limit_amount,
               p.starts_at, p.ends_at, p.name AS plan_name,
               c.name AS category_name
        FROM budgets b
        JOIN budget_plans p ON p.id = b.plan_id
        JOIN categories c ON c.id = b.category_id
        WHERE p.user_id = $1
          AND b.category_id = $2
          AND p.currency = $3
          AND p.starts_at::date <= $4::date
          AND p.ends_at::date >= $4::date
        """,
        user_id,
        category_id,
        currency,
        txn_date,
    )
    if not budgets:
        return

    for budget in budgets:
        if budget["limit_amount"] is None:
            continue  # uncapped - nothing to alert on

        consumed_after = await pool.fetchval(
            """
            SELECT COALESCE(SUM(a.amount), 0)
            FROM allocations a
            JOIN transactions t ON t.id = a.transaction_id
            WHERE a.category_id = $1
              AND a.currency = $2
              AND a.transfer_reason_id IS NULL
              AND t.txn_date >= $3::date
              AND t.txn_date <= $4::date
            """,
            category_id,
            currency,
            budget["starts_at"],
            budget["ends_at"],
        )
        consumed_before = consumed_after - new_amount

        before_state = (
            derive_budget_state(
                category_type="expense",
                limit_amount=budget["limit_amount"],
                consumed=consumed_before,
                near_threshold=near_threshold,
            )["state"]
            or "ok"
        )
        after_state = derive_budget_state(
            category_type="expense",
            limit_amount=budget["limit_amount"],
            consumed=consumed_after,
            near_threshold=near_threshold,
        )["state"]

        if after_state not in ("near", "at", "over"):
            continue
        if _STATE_RANK[after_state] <= _STATE_RANK.get(before_state, 0):
            continue

        title, body = _alert_copy(after_state, budget["category_name"], budget["plan_name"])
        await pool.execute(
            """
            INSERT INTO notifications (user_id, type, state, title, body, budget_id, plan_id)
            VALUES ($1, 'budget_alert', $2, $3, $4, $5, $6)
            """,
            user_id,
            after_state,
            title,
            body,
            budget["budget_id"],
            budget["plan_id"],
        )


async def list_notifications(
    pool: asyncpg.Pool,
    *,
    user_id: UUID,
    page: int,
    page_size: int,
    read_status: str = "all",
    from_date: date | None = None,
    to_date: date | None = None,
) -> tuple[list[dict], int, int]:
    """Newest-first page of a user's notifications (ab-75). unread_count is
    the user's TOTAL unread count regardless of the current page/filter -
    computed as its own query so a nav badge can show it without a second
    call, same purpose as budget_plans.py's own total_expenditure/
    total_income computed alongside each plan's per-category figures.

    `read_status` is "all" | "read" | "unread" - "all" (the default)
    applies no read_at filter at all. `from_date`/`to_date` filter by the
    notification's own created_at calendar date, inclusive on both ends,
    same `($N::date IS NULL OR ...)` optional-range pattern transactions.py
    uses.
    """
    unread_count = await pool.fetchval(
        "SELECT COUNT(*) FROM notifications WHERE user_id = $1 AND read_at IS NULL",
        user_id,
    )

    base_query = """
        FROM notifications
        WHERE user_id = $1
          AND ($2::text = 'all' OR ($2::text = 'unread') = (read_at IS NULL))
          AND ($3::date IS NULL OR created_at::date >= $3)
          AND ($4::date IS NULL OR created_at::date <= $4)
    """
    total = await pool.fetchval(f"SELECT COUNT(*) {base_query}", user_id, read_status, from_date, to_date)

    rows = await pool.fetch(
        f"""
        SELECT id, type, state, title, body, budget_id, plan_id, read_at, created_at
        {base_query}
        ORDER BY created_at DESC
        LIMIT $5 OFFSET $6
        """,
        user_id,
        read_status,
        from_date,
        to_date,
        page_size,
        (page - 1) * page_size,
    )

    return [dict(row) for row in rows], total, unread_count


async def mark_notification_read(
    pool: asyncpg.Pool,
    *,
    user_id: UUID,
    notification_id: UUID,
) -> dict | None:
    """Idempotent: read_at is only ever set once (COALESCE keeps an
    already-read notification's original read_at), but the row is matched
    and returned either way - no error re-marking an already-read
    notification. None if it doesn't exist or isn't owned by user_id, for
    the caller to turn into a 404 (mirrors update_budget's own convention).
    """
    row = await pool.fetchrow(
        """
        UPDATE notifications
        SET read_at = COALESCE(read_at, now())
        WHERE id = $1 AND user_id = $2
        RETURNING id, type, state, title, body, budget_id, plan_id, read_at, created_at
        """,
        notification_id,
        user_id,
    )
    return dict(row) if row else None
