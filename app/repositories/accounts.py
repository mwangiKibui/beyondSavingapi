from uuid import UUID

import asyncpg


class DuplicateAccount(Exception):
    pass


async def create_account(
    pool: asyncpg.Pool,
    *,
    user_id: UUID,
    nickname: str,
    account_type: str,
    provider: str,
    account_number: str,
    currency: str,
) -> dict:
    try:
        row = await pool.fetchrow(
            """
            INSERT INTO accounts (user_id, nickname, account_type, provider, account_number, currency)
            VALUES ($1, $2, $3, $4, $5, $6)
            RETURNING id, nickname, account_type, provider, account_number, currency,
                      is_active, created_at, updated_at
            """,
            user_id,
            nickname,
            account_type,
            provider,
            account_number,
            currency,
        )
    except asyncpg.UniqueViolationError as exc:
        raise DuplicateAccount(account_number) from exc
    return dict(row)
