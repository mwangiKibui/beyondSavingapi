from uuid import UUID

import asyncpg


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
