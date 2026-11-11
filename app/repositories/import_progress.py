"""statement_import_progress (ab-264): one row per unfinished import review.
Callers check the import belongs to the user first (get_import_for_preview)."""

import json
from uuid import UUID

import asyncpg

_COLUMNS = "step, draft, reviewed_count, total_count, updated_at"


def _row(record) -> dict | None:
    if record is None:
        return None
    out = dict(record)
    out["draft"] = json.loads(out["draft"])
    return out


async def get_progress(pool: asyncpg.Pool, *, import_id: UUID) -> dict | None:
    return _row(await pool.fetchrow(
        f"SELECT {_COLUMNS} FROM statement_import_progress WHERE import_id = $1", import_id
    ))


async def upsert_progress(
    pool: asyncpg.Pool, *, import_id: UUID, step: str, draft: dict, reviewed_count: int, total_count: int
) -> dict:
    return _row(await pool.fetchrow(
        f"""
        INSERT INTO statement_import_progress (import_id, step, draft, reviewed_count, total_count)
        VALUES ($1, $2, $3::jsonb, $4, $5)
        ON CONFLICT (import_id) DO UPDATE
        SET step = EXCLUDED.step, draft = EXCLUDED.draft, reviewed_count = EXCLUDED.reviewed_count,
            total_count = EXCLUDED.total_count, updated_at = now()
        RETURNING {_COLUMNS}
        """,
        import_id, step, json.dumps(draft), reviewed_count, total_count,
    ))


async def delete_progress(pool: asyncpg.Pool | asyncpg.Connection, *, import_id: UUID) -> bool:
    status = await pool.execute("DELETE FROM statement_import_progress WHERE import_id = $1", import_id)
    return status != "DELETE 0"
