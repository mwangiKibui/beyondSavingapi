"""Reads for the statement-import preview's category suggestions (ab-253).
Read-only: nothing here writes."""

from datetime import date
from uuid import UUID

import asyncpg


async def get_import_for_preview(pool: asyncpg.Pool, *, import_id: UUID, user_id: UUID) -> dict | None:
    # Scoped through accounts.user_id, like every other import read, so a
    # guessed import id from another user is simply not found.
    row = await pool.fetchrow(
        """
        SELECT si.id, si.account_id, a.nickname AS account_nickname, a.provider,
               si.file_name, si.status, si.period_start, si.period_end, si.row_count, si.created_at
        FROM statement_imports si
        JOIN accounts a ON a.id = si.account_id
        WHERE si.id = $1 AND a.user_id = $2
        """,
        import_id,
        user_id,
    )
    return dict(row) if row else None


async def get_import_rows(
    pool: asyncpg.Pool,
    *,
    import_id: UUID,
    account_id: UUID,
    period_start: date | None,
    period_end: date | None,
) -> list[dict]:
    """Every transaction the statement covers, with its allocated total
    (summed on original_amount, as get_transaction does) and, when its
    allocations all use one category, that category_id - used for
    "already chosen in this statement".

    "Covers" means rows this import inserted, plus statement rows already
    on the same account within its period: a re-uploaded or overlapping
    statement has its rows skipped by dedupe (they stay linked to the
    earlier import), but the user still expects to see them. When the
    import was limited to chosen sub-ledgers, only those count. Manual
    entries never do."""
    rows = await pool.fetch(
        """
        -- No is_transfer column ships (docs/schema.sql is ahead): a row is
        -- a transfer leg when it has a transfer_group_id.
        SELECT t.id, t.txn_date, t.amount, t.currency, t.direction, t.description,
               (t.transfer_group_id IS NOT NULL) AS is_transfer,
               COALESCE(alloc.total, 0) AS allocated,
               alloc.category_id
        FROM transactions t
        LEFT JOIN LATERAL (
            SELECT SUM(al.original_amount) AS total,
                   CASE WHEN COUNT(*) = COUNT(al.category_id) AND COUNT(DISTINCT al.category_id) = 1
                        THEN MIN(al.category_id::text)::uuid END AS category_id
            FROM allocations al
            WHERE al.transaction_id = t.id
        ) alloc ON true
        WHERE t.import_id = $1
           OR (
                t.account_id = $2
            AND t.source = 'statement'
            AND $3::date IS NOT NULL AND $4::date IS NOT NULL
            AND t.txn_date BETWEEN $3 AND $4
            AND (
                NOT EXISTS (SELECT 1 FROM statement_import_sub_ledgers s WHERE s.import_id = $1)
                OR t.sub_ledger_id IN (SELECT s.sub_ledger_id FROM statement_import_sub_ledgers s WHERE s.import_id = $1)
            )
           )
        ORDER BY t.txn_date, t.created_at, t.id
        """,
        import_id,
        account_id,
        period_start,
        period_end,
    )
    return [dict(row) for row in rows]


async def get_user_rules(pool: asyncpg.Pool, *, user_id: UUID, pairs: list[tuple[str, str]]) -> list[dict]:
    """The user's rules for the given (key_kind, key_value) pairs, in one
    query however many rows the statement has."""
    if not pairs:
        return []
    rows = await pool.fetch(
        """
        SELECT r.key_kind, r.key_value, r.provider, r.account_hash, r.direction, r.category_id,
               r.label, r.times_used, r.avg_amount
        FROM user_category_rules r
        JOIN unnest($2::text[], $3::text[]) AS k(key_kind, key_value)
          ON k.key_kind = r.key_kind AND k.key_value = r.key_value
        WHERE r.user_id = $1
        """,
        user_id,
        [kind for kind, _ in pairs],
        [value for _, value in pairs],
    )
    return [dict(row) for row in rows]


async def get_approved_directory(pool: asyncpg.Pool) -> list[dict]:
    """Every approved shared-list merchant, one row per key. A few hundred
    merchants, so matching happens in Python rather than per row in SQL."""
    rows = await pool.fetch(
        """
        SELECT m.id AS merchant_id, m.display_name, m.category_name, m.direction,
               k.key_kind, k.key_value, k.requires_text
        FROM merchant_directory m
        JOIN merchant_directory_keys k ON k.merchant_id = m.id AND k.direction = m.direction
        WHERE m.status = 'approved'
        """
    )
    return [dict(row) for row in rows]


async def get_blocked_numbers(pool: asyncpg.Pool) -> list[dict]:
    rows = await pool.fetch("SELECT number_kind, number FROM blocked_numbers")
    return [dict(row) for row in rows]


async def get_user_categories(pool: asyncpg.Pool, *, user_id: UUID) -> list[dict]:
    rows = await pool.fetch("SELECT id, name, type FROM categories WHERE user_id = $1", user_id)
    return [dict(row) for row in rows]
