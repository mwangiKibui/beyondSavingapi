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
            si.error_detail, si.created_at
        FROM statement_imports si
        JOIN accounts a ON a.id = si.account_id
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
) -> None:
    await pool.execute(
        """
        UPDATE statement_imports
        SET status = 'parsed', period_start = $1, period_end = $2, row_count = $3
        WHERE id = $4
        """,
        period_start,
        period_end,
        row_count,
        import_id,
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
