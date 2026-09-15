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


async def consume_password_reset_token(
    pool: asyncpg.Pool, *, token: str, new_password_hash: str
) -> bool:
    """Atomically validates a token (exists, unused, unexpired), marks it
    used, and updates the owning user's password - all in one transaction,
    so a token can never be consumed twice even under concurrent requests,
    and a failure partway through never leaves a used-but-ineffective token.
    Returns whether the token was valid.
    """
    async with pool.acquire() as conn:
        async with conn.transaction():
            row = await conn.fetchrow(
                """
                UPDATE password_reset_tokens
                SET used_at = now()
                WHERE token = $1 AND used_at IS NULL AND expires_at > now()
                RETURNING user_id
                """,
                token,
            )
            if row is None:
                return False

            await conn.execute(
                "UPDATE users SET password_hash = $1, updated_at = now() WHERE id = $2",
                new_password_hash,
                row["user_id"],
            )
    return True
