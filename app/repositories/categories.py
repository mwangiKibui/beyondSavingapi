from uuid import UUID

import asyncpg


class DuplicateCategory(Exception):
    pass


# Whitelisted, not user-supplied directly - Pydantic's Literal already
# constrains sort_by before this is reached, but keeping the SQL fragment
# itself off free-form input is worth the belt-and-braces (matches
# accounts.py's _SORT_COLUMNS).
_SORT_COLUMNS = {
    "name": "name",
}


async def create_category(
    pool: asyncpg.Pool,
    *,
    user_id: UUID,
    name: str,
    category_type: str,
) -> dict:
    try:
        row = await pool.fetchrow(
            """
            INSERT INTO categories (user_id, name, type)
            VALUES ($1, $2, $3)
            RETURNING id, name, type, is_default, created_at
            """,
            user_id,
            name,
            category_type,
        )
    except asyncpg.UniqueViolationError as exc:
        raise DuplicateCategory() from exc
    return dict(row)


async def update_category(
    pool: asyncpg.Pool,
    *,
    category_id: UUID,
    user_id: UUID,
    name: str,
) -> dict | None:
    try:
        row = await pool.fetchrow(
            """
            UPDATE categories
            SET name = $1
            WHERE id = $2 AND user_id = $3
            RETURNING id, name, type, is_default, created_at
            """,
            name,
            category_id,
            user_id,
        )
    except asyncpg.UniqueViolationError as exc:
        raise DuplicateCategory() from exc
    return dict(row) if row else None


async def list_categories(
    pool: asyncpg.Pool,
    *,
    user_id: UUID,
    category_type: str,
    page: int,
    page_size: int,
    search: str | None,
    sort_by: str,
    sort_dir: str,
) -> tuple[list[dict], int]:
    base_query = """
        SELECT id, name, type, is_default
        FROM categories
        WHERE user_id = $1
          AND type = $2
          AND ($3::text IS NULL OR name ILIKE '%' || $3 || '%')
    """
    params = [user_id, category_type, search]

    count_row = await pool.fetchrow(
        f"SELECT COUNT(*) AS total FROM ({base_query}) counted", *params
    )
    total = count_row["total"]

    sort_column = _SORT_COLUMNS[sort_by]
    order_direction = "ASC" if sort_dir == "asc" else "DESC"
    paged_query = f"""
        {base_query}
        ORDER BY {sort_column} {order_direction}
        LIMIT ${len(params) + 1} OFFSET ${len(params) + 2}
    """
    rows = await pool.fetch(paged_query, *params, page_size, (page - 1) * page_size)

    return [dict(row) for row in rows], total
