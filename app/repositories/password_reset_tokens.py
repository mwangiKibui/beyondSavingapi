from datetime import datetime
from uuid import UUID

import asyncpg


async def create_password_reset_token(
    pool: asyncpg.Pool, *, user_id: UUID, token: str, expires_at: datetime
) -> None:
    async with pool.acquire() as conn:
        async with conn.transaction():
            # Invalidate any still-unused tokens for this user before issuing
            # a new one, so only the latest request is ever valid.
            await conn.execute(
                "DELETE FROM password_reset_tokens WHERE user_id = $1 AND used_at IS NULL",
                user_id,
            )
            await conn.execute(
                """
                INSERT INTO password_reset_tokens (user_id, token, expires_at)
                VALUES ($1, $2, $3)
                """,
                user_id,
                token,
                expires_at,
            )
