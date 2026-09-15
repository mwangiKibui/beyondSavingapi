import asyncpg


class EmailAlreadyExists(Exception):
    pass


async def create_user(
    pool: asyncpg.Pool, *, email: str, first_name: str, last_name: str, password_hash: str
) -> dict:
    try:
        row = await pool.fetchrow(
            """
            INSERT INTO users (email, first_name, last_name, password_hash)
            VALUES ($1, $2, $3, $4)
            RETURNING id, email, first_name, last_name, default_currency, near_threshold, created_at
            """,
            email,
            first_name,
            last_name,
            password_hash,
        )
    except asyncpg.UniqueViolationError as exc:
        raise EmailAlreadyExists(email) from exc
    return dict(row)
