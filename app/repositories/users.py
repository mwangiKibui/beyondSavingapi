from uuid import UUID

import asyncpg
import bcrypt

from app.repositories.categories import seed_default_categories
from app.repositories.transfer_reasons import seed_default_transfer_reasons


class EmailAlreadyExists(Exception):
    pass


class IncorrectPassword(Exception):
    """Raised by update_user_password when current_password doesn't match
    the stored hash - the API layer (app/api/users.py) catches this and
    translates it to a 401, mirroring the OverlappingBudgetPlan /
    DuplicateCategory custom-exception-to-HTTP-error convention used
    elsewhere."""


async def create_user(
    pool: asyncpg.Pool, *, email: str, first_name: str, last_name: str, password_hash: str
) -> dict:
    # One transaction: a new user always gets their default categories and
    # transfer reasons, or none of them exist - a signup that fails
    # partway through never leaves a user stranded without them.
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
            await seed_default_transfer_reasons(conn, user_id=row["id"])

    return dict(row)


async def get_user_by_email(pool: asyncpg.Pool, email: str) -> dict | None:
    row = await pool.fetchrow(
        "SELECT id, email, password_hash FROM users WHERE email = $1",
        email,
    )
    return dict(row) if row else None


async def get_user_by_id(pool: asyncpg.Pool, user_id: UUID) -> dict | None:
    row = await pool.fetchrow(
        """
        SELECT id, email, first_name, last_name, default_currency, near_threshold, created_at, role
        FROM users
        WHERE id = $1
        """,
        user_id,
    )
    return dict(row) if row else None


async def list_users(
    pool: asyncpg.Pool, *, page: int, page_size: int, search: str | None
) -> tuple[list[dict], int]:
    """Paginated/searchable user list for the admin panel - same
    count-then-page + conditional-filter shape as list_accounts (see
    app/repositories/accounts.py). search matches against email or full
    name."""
    base_query = """
        SELECT id, email, first_name, last_name, role, created_at
        FROM users
        WHERE
            $1::text IS NULL
            OR email ILIKE '%' || $1 || '%'
            OR (first_name || ' ' || last_name) ILIKE '%' || $1 || '%'
    """
    params = [search]

    count_row = await pool.fetchrow(
        f"SELECT COUNT(*) AS total FROM ({base_query}) counted", *params
    )
    total = count_row["total"]

    rows = await pool.fetch(
        f"""
        {base_query}
        ORDER BY created_at DESC
        LIMIT ${len(params) + 1} OFFSET ${len(params) + 2}
        """,
        *params,
        page_size,
        (page - 1) * page_size,
    )
    return [dict(row) for row in rows], total


async def update_user_profile(
    pool: asyncpg.Pool, *, user_id: UUID, first_name: str, last_name: str, email: str
) -> dict | None:
    try:
        row = await pool.fetchrow(
            """
            UPDATE users
            SET first_name = $1, last_name = $2, email = $3, updated_at = now()
            WHERE id = $4
            RETURNING id, email, first_name, last_name, default_currency, near_threshold, created_at, role
            """,
            first_name,
            last_name,
            email,
            user_id,
        )
    except asyncpg.UniqueViolationError as exc:
        raise EmailAlreadyExists(email) from exc
    return dict(row) if row else None


async def update_user_password(
    pool: asyncpg.Pool, *, user_id: UUID, current_password: str, new_password_hash: str
) -> bool:
    """Verifies current_password against the stored hash and, if it
    matches, persists new_password_hash. Returns False if the user no
    longer exists; raises IncorrectPassword if current_password is wrong.
    The verification happens here (rather than in the API layer, as
    login's does) because it needs the stored hash that only this query
    fetches - bundling fetch+verify+update into one call keeps the
    "wrong password" case a single domain exception the API layer can
    translate to a 401, the same way it translates EmailAlreadyExists to
    a 409."""
    row = await pool.fetchrow("SELECT password_hash FROM users WHERE id = $1", user_id)
    if row is None:
        return False

    if not bcrypt.checkpw(current_password.encode(), row["password_hash"].encode()):
        raise IncorrectPassword()

    await pool.execute(
        "UPDATE users SET password_hash = $1, updated_at = now() WHERE id = $2",
        new_password_hash,
        user_id,
    )
    return True


async def update_user_preferences(
    pool: asyncpg.Pool, *, user_id: UUID, default_currency: str, near_threshold: float
) -> dict | None:
    row = await pool.fetchrow(
        """
        UPDATE users
        SET default_currency = $1, near_threshold = $2, updated_at = now()
        WHERE id = $3
        RETURNING id, email, first_name, last_name, default_currency, near_threshold, created_at, role
        """,
        default_currency,
        near_threshold,
        user_id,
    )
    return dict(row) if row else None
