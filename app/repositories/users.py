import asyncpg


class EmailAlreadyExists(Exception):
    pass


async def create_user(pool: asyncpg.Pool, *, email: str, name: str, password_hash: str) -> dict:
    try:
        row = await pool.fetchrow(
            """
            INSERT INTO users (email, name, password_hash)
            VALUES ($1, $2, $3)
            RETURNING id, email, name, default_currency, near_threshold, created_at
            """,
            email,
            name,
            password_hash,
        )
    except asyncpg.UniqueViolationError as exc:
        raise EmailAlreadyExists(email) from exc
    return dict(row)
