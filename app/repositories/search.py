"""SQL for the navbar search (ab-274). Every query is scoped to the signed-in
user (accounts.user_id / money_plans.user_id) - nothing reads another
user's rows.

pattern is an ILIKE pattern from app.services.search.like_pattern ('\\'
escapes). Transactions are narrowed to the user's account ids FIRST
(`t.account_id = ANY(ARRAY(...))`), which uses idx_txn_account_date: joined
on accounts instead, the planner seq-scans every user's rows (~170 ms on
55k rows locally vs ~3-7 ms). A user has a few thousand rows, so no text
index is needed; see the README's Search section.
"""

from decimal import Decimal
from uuid import UUID

import asyncpg

# Newest matching statement rows keyed into payees, at most. Keying is
# Python CPU on the event loop (~14 µs a row: 500 rows ~7 ms), so it stays bounded.
PAYEE_ROW_CAP = 500

_USERS_ACCOUNTS = "t.account_id = ANY(ARRAY(SELECT id FROM accounts WHERE user_id = $1))"


async def _paged(conn, sql: str, count_sql: str, params: list, limit: int, offset: int) -> tuple[list[dict], int]:
    """sql selects count(*) OVER () AS total_count; a page past the end has
    no rows to carry it, so count separately then."""
    n = len(params)
    rows = await conn.fetch(f"{sql} LIMIT ${n + 1} OFFSET ${n + 2}", *params, limit, offset)
    if rows:
        return [dict(r) for r in rows], rows[0]["total_count"]
    total = await conn.fetchval(count_sql, *params) if offset else 0
    return [], total


async def search_transactions(
    conn: asyncpg.Connection, *, user_id: UUID, pattern: str, amount: Decimal | None, limit: int, offset: int
) -> tuple[list[dict], int]:
    where = f"""
        FROM transactions t
        JOIN accounts a ON a.id = t.account_id
        WHERE {_USERS_ACCOUNTS}
          AND (t.description ILIKE $2
               OR t.counterparty ILIKE $2
               OR ($3::numeric IS NOT NULL AND t.amount = $3))
    """
    sql = f"""
        SELECT t.id, t.account_id, a.nickname AS account_nickname, t.txn_date, t.amount, t.currency,
               t.direction, t.description, t.counterparty, count(*) OVER () AS total_count
        {where}
        ORDER BY t.txn_date DESC, t.created_at DESC, t.id
    """
    return await _paged(conn, sql, f"SELECT count(*) {where}", [user_id, pattern, amount], limit, offset)


async def search_payee_rows(conn: asyncpg.Connection, *, user_id: UUID, pattern: str) -> list[dict]:
    """The user's newest statement rows whose text matches, for grouping
    into payees (app.services.search.payee_items)."""
    rows = await conn.fetch(
        f"""
        SELECT t.id, t.txn_date, t.amount, t.currency, t.direction, t.description, a.provider
        FROM transactions t
        JOIN accounts a ON a.id = t.account_id
        WHERE {_USERS_ACCOUNTS}
          AND t.source = 'statement'
          AND t.description ILIKE $2
        ORDER BY t.txn_date DESC, t.id
        LIMIT $3
        """,
        user_id,
        pattern,
        PAYEE_ROW_CAP,
    )
    return [dict(r) for r in rows]


async def search_money_plans(
    conn: asyncpg.Connection, *, user_id: UUID, pattern: str, limit: int, offset: int
) -> tuple[list[dict], int]:
    where = "FROM money_plans p WHERE p.user_id = $1 AND p.name ILIKE $2"
    sql = f"""
        SELECT p.id, p.name, p.period, p.status, p.starts_at, count(*) OVER () AS total_count
        {where}
        ORDER BY p.starts_at DESC, p.id
    """
    return await _paged(conn, sql, f"SELECT count(*) {where}", [user_id, pattern], limit, offset)


async def search_accounts(
    conn: asyncpg.Connection, *, user_id: UUID, pattern: str, limit: int, offset: int
) -> tuple[list[dict], int]:
    # Matched on what the user named it and who provides it - never the
    # account number.
    where = "FROM accounts a WHERE a.user_id = $1 AND (a.nickname ILIKE $2 OR a.provider ILIKE $2)"
    sql = f"""
        SELECT a.id, a.nickname, a.provider, a.account_type, a.is_active, count(*) OVER () AS total_count
        {where}
        ORDER BY a.is_active DESC, lower(a.nickname), a.id
    """
    return await _paged(conn, sql, f"SELECT count(*) {where}", [user_id, pattern], limit, offset)
