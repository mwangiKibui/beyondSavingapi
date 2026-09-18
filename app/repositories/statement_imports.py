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


async def mark_import_failed(pool: asyncpg.Pool, *, import_id: UUID, error_detail: str) -> None:
    await pool.execute(
        "UPDATE statement_imports SET status = 'failed', error_detail = $1 WHERE id = $2",
        error_detail,
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
