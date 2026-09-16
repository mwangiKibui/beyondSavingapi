import asyncpg

from app.repositories.categories import seed_default_categories


class EmailAlreadyExists(Exception):
    pass


async def create_user(
    pool: asyncpg.Pool, *, email: str, first_name: str, last_name: str, password_hash: str
) -> dict:
    # One transaction: a new user always gets their default categories, or
    # neither exists - a signup that fails partway through never leaves a
    # user stranded without them.
    async with pool.acquire() as conn:
        async with conn.transaction():
            try:
                row = await conn.fetchrow(
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

            await seed_default_categories(conn, user_id=row["id"])

    return dict(row)


async def get_user_by_email(pool: asyncpg.Pool, email: str) -> dict | None:
    row = await pool.fetchrow(
        "SELECT id, email, password_hash FROM users WHERE email = $1",
        email,
    )
    return dict(row) if row else None
