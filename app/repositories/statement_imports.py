from datetime import date
from uuid import UUID

import asyncpg


async def list_statement_imports(
    pool: asyncpg.Pool,
    *,
    user_id: UUID,
    page: int,
    page_size: int,
    search: str | None,
    status: str | None,
    account_id: UUID | None,
    sort_dir: str,
) -> tuple[list[dict], int]:
    # Joined through accounts.user_id - never scoped to statement_imports
    # directly, so a user can never see another user's imports even if
    # they guess an import id or account id.
    base_query = """
        SELECT
            si.id, si.account_id, a.nickname AS account_nickname, si.file_name,
            si.status, si.period_start, si.period_end, si.row_count,
            si.error_detail, si.created_at, si.reviewed_at, si.allocated_at,
            -- ab-297: rows this upload created; 0 from a parsed statement
            -- means every row was already recorded by an earlier upload.
            CASE WHEN si.status = 'parsed'
                 THEN (SELECT COUNT(*) FROM transactions t WHERE t.import_id = si.id) END AS added_count,
            p.step AS progress_step, p.reviewed_count AS progress_reviewed_count,
            p.total_count AS progress_total_count, p.updated_at AS progress_updated_at
        FROM statement_imports si
        JOIN accounts a ON a.id = si.account_id
        LEFT JOIN statement_import_progress p ON p.import_id = si.id
        WHERE a.user_id = $1
          AND ($2::text IS NULL OR si.file_name ILIKE '%' || $2 || '%')
          AND ($3::text IS NULL OR si.status = $3)
          AND ($4::uuid IS NULL OR si.account_id = $4)
    """
    params = [user_id, search, status, account_id]

    count_row = await pool.fetchrow(
        f"SELECT COUNT(*) AS total FROM ({base_query}) counted", *params
    )
    total = count_row["total"]

    order_direction = "ASC" if sort_dir == "asc" else "DESC"
    paged_query = f"""
        {base_query}
        ORDER BY si.created_at {order_direction}
        LIMIT ${len(params) + 1} OFFSET ${len(params) + 2}
    """
    rows = await pool.fetch(paged_query, *params, page_size, (page - 1) * page_size)

    return [dict(row) for row in rows], total


async def get_import_owned_by_user(pool: asyncpg.Pool, *, import_id: UUID, user_id: UUID) -> dict | None:
    """Confirms an import_id both exists and belongs to this user, for
    ab-51's List-transactions API to 404 on a guessed/foreign import_id
    the same way it already does for account_id (via get_account).
    """
    row = await pool.fetchrow(
        """
        SELECT si.id
        FROM statement_imports si
        JOIN accounts a ON a.id = si.account_id
        WHERE si.id = $1 AND a.user_id = $2
        """,
        import_id,
        user_id,
    )
    return dict(row) if row else None


async def get_import_for_processing(pool: asyncpg.Pool, *, import_id: UUID) -> dict | None:
    row = await pool.fetchrow(
        """
        SELECT si.id, si.account_id, si.storage_key, si.file_name, a.provider
        FROM statement_imports si
        JOIN accounts a ON a.id = si.account_id
        WHERE si.id = $1
        """,
        import_id,
    )
    return dict(row) if row else None


async def get_import_sub_ledgers(pool: asyncpg.Pool, *, import_id: UUID) -> list[dict]:
    """The sub-ledgers the uploader selected for this import (ab-119) -
    empty for every import against an account with no sub-ledgers.
    """
    rows = await pool.fetch(
        """
        SELECT sl.id, sl.name
        FROM statement_import_sub_ledgers sis
        JOIN sub_ledgers sl ON sl.id = sis.sub_ledger_id
        WHERE sis.import_id = $1
        """,
        import_id,
    )
    return [dict(row) for row in rows]


async def add_import_sub_ledgers(pool: asyncpg.Pool, *, import_id: UUID, sub_ledger_ids: list[UUID]) -> None:
    if not sub_ledger_ids:
        return
    await pool.executemany(
        "INSERT INTO statement_import_sub_ledgers (import_id, sub_ledger_id) VALUES ($1, $2)",
        [(import_id, sub_ledger_id) for sub_ledger_id in sub_ledger_ids],
    )


async def get_import_overlap(pool: asyncpg.Pool, *, import_id: UUID) -> dict:
    """ab-297: of the rows an upload's review lists (its own, plus statement
    rows on the account in its period - see get_import_rows), how many it
    added and how many an earlier upload had already recorded, and the
    earlier upload holding most of them. Callers check ownership first."""
    row = await pool.fetchrow(
        """
        WITH si AS (SELECT * FROM statement_imports WHERE id = $1),
        rows AS (
            SELECT t.import_id
            FROM transactions t, si
            WHERE t.import_id = $1
               OR (
                    t.account_id = si.account_id
                AND t.source = 'statement'
                AND si.period_start IS NOT NULL AND si.period_end IS NOT NULL
                AND t.txn_date BETWEEN si.period_start AND si.period_end
                AND (
                    NOT EXISTS (SELECT 1 FROM statement_import_sub_ledgers s WHERE s.import_id = $1)
                    OR t.sub_ledger_id IN (SELECT s.sub_ledger_id FROM statement_import_sub_ledgers s WHERE s.import_id = $1)
                )
               )
        ),
        earlier AS (
            SELECT import_id FROM rows
            WHERE import_id IS NOT NULL AND import_id <> $1
            GROUP BY import_id
            ORDER BY COUNT(*) DESC, import_id
            LIMIT 1
        )
        SELECT
            (SELECT COUNT(*) FROM rows WHERE import_id = $1) AS added_count,
            (SELECT COUNT(*) FROM rows WHERE import_id IS DISTINCT FROM $1) AS already_count,
            e.id AS earlier_id, e.file_name AS earlier_file_name, e.created_at AS earlier_uploaded_at
        FROM (SELECT 1) one
        LEFT JOIN earlier ON true
        LEFT JOIN statement_imports e ON e.id = earlier.import_id
        """,
        import_id,
    )
    earlier = None
    if row["earlier_id"] is not None:
        earlier = {"import_id": row["earlier_id"], "file_name": row["earlier_file_name"],
                   "uploaded_at": row["earlier_uploaded_at"]}
    return {"added_count": row["added_count"], "already_count": row["already_count"], "earlier": earlier}


async def delete_import_without_rows(pool: asyncpg.Pool, *, import_id: UUID) -> str | None:
    """ab-297: removes an upload that added no transactions (a re-upload of
    a statement already recorded). Returns its stored file's key, or None
    when it has rows of its own and was kept. Callers check ownership."""
    return await pool.fetchval(
        """
        DELETE FROM statement_imports si
        WHERE si.id = $1 AND NOT EXISTS (SELECT 1 FROM transactions t WHERE t.import_id = si.id)
        RETURNING si.storage_key
        """,
        import_id,
    )


async def set_import_allocated(pool: asyncpg.Pool, *, import_id: UUID, allocated: bool):
    """ab-296: the user moved on from Allocation to Review (or back).
    Callers check the import is the user's first."""
    return await pool.fetchval(
        "UPDATE statement_imports SET allocated_at = CASE WHEN $2 THEN now() END WHERE id = $1 RETURNING allocated_at",
        import_id,
        allocated,
    )


async def mark_import_failed(pool: asyncpg.Pool, *, import_id: UUID, error_detail: str) -> None:
    await pool.execute(
        "UPDATE statement_imports SET status = 'failed', error_detail = $1 WHERE id = $2",
        error_detail,
        import_id,
    )


async def mark_import_parsed(
    pool: asyncpg.Pool,
    *,
    import_id: UUID,
    period_start: date | None,
    period_end: date | None,
    row_count: int,
    opening_balance: float | None = None,
    opening_balance_date: date | None = None,
) -> None:
    await pool.execute(
        """
        UPDATE statement_imports
        SET status = 'parsed', period_start = $1, period_end = $2, row_count = $3,
            opening_balance = $4, opening_balance_date = $5
        WHERE id = $6
        """,
        period_start,
        period_end,
        row_count,
        opening_balance,
        opening_balance_date,
        import_id,
    )


async def set_sub_ledger_opening_balances(
    pool: asyncpg.Pool, *, import_id: UUID, opening_balances: list[tuple[UUID, float, date]]
) -> None:
    """Records each selected sub-ledger's own opening balance (book
    balance's BBF anchor) against the statement_import_sub_ledgers row
    add_import_sub_ledgers already inserted for this import - one entry
    per (sub_ledger_id, opening_balance, opening_balance_date) tuple.
    """
    if not opening_balances:
        return
    await pool.executemany(
        """
        UPDATE statement_import_sub_ledgers
        SET opening_balance = $1, opening_balance_date = $2
        WHERE import_id = $3 AND sub_ledger_id = $4
        """,
        [
            (opening_balance, opening_balance_date, import_id, sub_ledger_id)
            for sub_ledger_id, opening_balance, opening_balance_date in opening_balances
        ],
    )


async def create_statement_import(
    pool: asyncpg.Pool,
    *,
    import_id: UUID,
    account_id: UUID,
    file_name: str,
    storage_key: str,
) -> dict:
    row = await pool.fetchrow(
        """
        INSERT INTO statement_imports (id, account_id, file_name, storage_key)
        VALUES ($1, $2, $3, $4)
        RETURNING id, account_id, file_name, status, period_start, period_end,
                  row_count, error_detail, storage_key, created_at
        """,
        import_id,
        account_id,
        file_name,
        storage_key,
    )
    return dict(row)
