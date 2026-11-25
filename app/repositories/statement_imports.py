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
        SELECT si.id, si.account_id, si.storage_key, si.file_name, si.status, si.fingerprint,
               si.profile_id, a.provider, a.currency, a.user_id, i.id AS institution_id
        FROM statement_imports si
        JOIN accounts a ON a.id = si.account_id
        -- ab-312: the institution whose profiles may read this file
        -- (matched by name + type; accounts.provider has no FK).
        LEFT JOIN institutions i ON i.name = a.provider AND i.type = a.account_type
        WHERE si.id = $1
        """,
        import_id,
    )
    return dict(row) if row else None


async def set_import_fingerprint(pool: asyncpg.Pool, *, import_id: UUID, fingerprint: str) -> None:
    """ab-312: the upload's page-1 layout shape (never its values), kept so
    an upload no profile could read can be re-matched once one exists."""
    await pool.execute("UPDATE statement_imports SET fingerprint = $1 WHERE id = $2", fingerprint, import_id)


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


# ab-297/ab-301: the rows an upload's review lists (its own, plus statement
# rows on the account in its period - see get_import_rows) and the earlier
# upload holding most of them. Shared by the progress endpoint's
# already_recorded_by and by removing a duplicate, so the two never drift.
_OVERLAP_CTES = """
        si AS (SELECT * FROM statement_imports WHERE id = $1),
        covered AS (
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
            SELECT import_id FROM covered
            WHERE import_id IS NOT NULL AND import_id <> $1
            GROUP BY import_id
            ORDER BY COUNT(*) DESC, import_id
            LIMIT 1
        )"""


async def get_import_overlap(pool: asyncpg.Pool, *, import_id: UUID) -> dict:
    """ab-297: of the rows an upload's review lists, how many it added and
    how many an earlier upload had already recorded, and the earlier upload
    holding most of them. Callers check ownership first."""
    row = await pool.fetchrow(
        f"""
        WITH {_OVERLAP_CTES}
        SELECT
            (SELECT COUNT(*) FROM covered WHERE import_id = $1) AS added_count,
            (SELECT COUNT(*) FROM covered WHERE import_id IS DISTINCT FROM $1) AS already_count,
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


async def move_applies_to_earlier(conn: asyncpg.Connection, *, import_id: UUID) -> int:
    """ab-301: points a duplicate upload's allocation batches at the earlier
    upload, so their batch Undo shows in that upload's review. Each batch
    goes to the upload holding most of the rows it filed (same account,
    oldest upload on a tie); a batch with no rows left goes to the upload
    already_recorded_by names. With no earlier upload a batch is left
    alone, and the delete's ON DELETE SET NULL applies as before. Undone
    batches move too, keeping the history together. Returns how many moved."""
    status = await conn.execute(
        f"""
        WITH {_OVERLAP_CTES},
        by_rows AS (
            SELECT DISTINCT ON (al.apply_id) al.apply_id, t.import_id
            FROM category_applies ca
            JOIN allocations al ON al.apply_id = ca.id
            JOIN transactions t ON t.id = al.transaction_id
            JOIN statement_imports e ON e.id = t.import_id
            JOIN si ON e.account_id = si.account_id
            WHERE ca.import_id = $1 AND t.import_id <> $1
            GROUP BY al.apply_id, t.import_id, e.created_at
            ORDER BY al.apply_id, COUNT(*) DESC, e.created_at, t.import_id
        ),
        targets AS (
            SELECT ca.id, COALESCE(b.import_id, (SELECT import_id FROM earlier)) AS import_id
            FROM category_applies ca
            LEFT JOIN by_rows b ON b.apply_id = ca.id
            WHERE ca.import_id = $1
        )
        UPDATE category_applies ca
        SET import_id = tg.import_id
        FROM targets tg
        WHERE ca.id = tg.id AND tg.import_id IS NOT NULL
        """,
        import_id,
    )
    return int(status.split()[-1])


async def delete_import_without_rows(pool: asyncpg.Pool, *, import_id: UUID) -> str | None:
    """ab-297: removes an upload that added no transactions (a re-upload of
    a statement already recorded). Returns its stored file's key, or None
    when it has rows of its own and was kept. Callers check ownership.

    ab-301: in the same transaction, its allocation batches first move to
    the earlier upload (see move_applies_to_earlier)."""
    async with pool.acquire() as conn, conn.transaction():
        removable = await conn.fetchval(
            """
            SELECT si.id FROM statement_imports si
            WHERE si.id = $1 AND NOT EXISTS (SELECT 1 FROM transactions t WHERE t.import_id = si.id)
            FOR UPDATE
            """,
            import_id,
        )
        if removable is None:
            return None
        await move_applies_to_earlier(conn, import_id=import_id)
        return await conn.fetchval(
            "DELETE FROM statement_imports WHERE id = $1 RETURNING storage_key",
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
    profile_id: UUID | None = None,
) -> None:
    """profile_id: the statement profile that read the file (ab-312);
    None when a bespoke parser read it."""
    await pool.execute(
        """
        UPDATE statement_imports
        SET status = 'parsed', period_start = $1, period_end = $2, row_count = $3,
            opening_balance = $4, opening_balance_date = $5, profile_id = $7
        WHERE id = $6
        """,
        period_start,
        period_end,
        row_count,
        opening_balance,
        opening_balance_date,
        import_id,
        profile_id,
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


# --- ab-315: the mapping wizard ---------------------------------------------------


async def set_import_status(pool, *, import_id: UUID, status: str) -> None:
    """needs_mapping / awaiting_support / pending - a state change that
    carries no failure message."""
    await pool.execute(
        "UPDATE statement_imports SET status = $1, error_detail = NULL WHERE id = $2", status, import_id
    )


async def get_import_for_layout(pool, *, import_id: UUID, user_id: UUID) -> dict | None:
    """The uploader's own import, with what the wizard needs."""
    row = await pool.fetchrow(
        """
        SELECT si.id, si.status, si.storage_key, si.file_name, si.fingerprint,
               a.provider, a.currency, i.id AS institution_id
        FROM statement_imports si
        JOIN accounts a ON a.id = si.account_id
        LEFT JOIN institutions i ON i.name = a.provider AND i.type = a.account_type
        WHERE si.id = $1 AND a.user_id = $2
        """,
        import_id,
        user_id,
    )
    return dict(row) if row else None


async def list_waiting_imports(pool, *, institution_id: UUID) -> list[UUID]:
    """Uploads of this institution waiting for support - to re-read once a
    layout for it becomes active."""
    rows = await pool.fetch(
        """
        SELECT si.id
        FROM statement_imports si
        JOIN accounts a ON a.id = si.account_id
        JOIN institutions i ON i.name = a.provider AND i.type = a.account_type
        WHERE si.status = 'awaiting_support' AND i.id = $1
        ORDER BY si.created_at
        """,
        institution_id,
    )
    return [r["id"] for r in rows]


async def queue_with_layout(pool, *, import_id: UUID, profile_id: UUID, fingerprint: str | None) -> None:
    """The uploader saved a layout for this upload: it is read next with that
    exact layout (profile_id), and its fingerprint is the one the layout was
    saved under, taken from the file by the current code."""
    await pool.execute(
        """
        UPDATE statement_imports
        SET status = 'pending', error_detail = NULL, profile_id = $2,
            fingerprint = COALESCE($3, fingerprint)
        WHERE id = $1
        """,
        import_id,
        profile_id,
        fingerprint,
    )


async def return_to_mapping(pool, *, import_id: UUID, error_detail: str) -> None:
    """The saved layout didn't read the whole file: back to Columns, saying why."""
    await pool.execute(
        """
        UPDATE statement_imports
        SET status = 'needs_mapping', error_detail = $2, profile_id = NULL
        WHERE id = $1
        """,
        import_id,
        error_detail,
    )
