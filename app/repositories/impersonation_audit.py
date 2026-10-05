from uuid import UUID

import asyncpg


async def create_impersonation_session(
    pool: asyncpg.Pool, *, admin_id: UUID, target_user_id: UUID
) -> UUID:
    row = await pool.fetchrow(
        """
        INSERT INTO impersonation_audit_log (admin_id, target_user_id)
        VALUES ($1, $2)
        RETURNING id
        """,
        admin_id,
        target_user_id,
    )
    return row["id"]


async def end_impersonation_session(pool: asyncpg.Pool, *, session_id: UUID) -> None:
    await pool.execute(
        "UPDATE impersonation_audit_log SET ended_at = now() WHERE id = $1 AND ended_at IS NULL",
        session_id,
    )
