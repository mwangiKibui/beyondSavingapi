"""Description keywords (ab-290) and users' private keyword choices.

A user's choice is theirs alone; only the category NAME counts (as a
number of users) towards what's suggested to everyone.
"""

from uuid import UUID

import asyncpg

_KEYWORD_COLUMNS = "id, pattern, direction, category_name, status, source, created_at, updated_at"


async def get_active_keywords(conn: asyncpg.Connection | asyncpg.Pool) -> list[dict]:
    rows = await conn.fetch(
        "SELECT id, pattern, direction, category_name, target FROM description_keywords WHERE status = 'approved'"
    )
    return [dict(r) for r in rows]


async def get_keyword_choices(
    conn: asyncpg.Connection | asyncpg.Pool, *, user_id: UUID, keyword_ids: list[UUID]
) -> dict[UUID, UUID]:
    if not keyword_ids:
        return {}
    rows = await conn.fetch(
        "SELECT keyword_id, category_id FROM user_keyword_choices WHERE user_id = $1 AND keyword_id = ANY($2::uuid[])",
        user_id, keyword_ids,
    )
    return {r["keyword_id"]: r["category_id"] for r in rows}


async def get_shared_keyword_choices(
    conn: asyncpg.Connection | asyncpg.Pool, *, keyword_ids: list[UUID]
) -> dict[UUID, list[dict]]:
    """Per keyword: each category name users filed it under, and how many
    users. Counts only - never who."""
    if not keyword_ids:
        return {}
    rows = await conn.fetch(
        """
        SELECT ukc.keyword_id, min(btrim(c.name)) AS category_name, count(DISTINCT ukc.user_id)::int AS users
        FROM user_keyword_choices ukc
        JOIN categories c ON c.id = ukc.category_id
        WHERE ukc.keyword_id = ANY($1::uuid[])
        GROUP BY ukc.keyword_id, lower(btrim(c.name))
        """,
        keyword_ids,
    )
    out: dict[UUID, list[dict]] = {}
    for r in rows:
        out.setdefault(r["keyword_id"], []).append({"category_name": r["category_name"], "users": r["users"]})
    return out


async def save_keyword_choices(
    conn: asyncpg.Connection, *, user_id: UUID, keyword_ids: list[UUID], category_id: UUID
) -> None:
    """The user filed these keywords' rows under category_id (not the list's
    category). Choosing the same again counts up; a new one starts over."""
    if not keyword_ids:
        return
    await conn.executemany(
        """
        INSERT INTO user_keyword_choices AS u (user_id, keyword_id, category_id)
        VALUES ($1, $2, $3)
        ON CONFLICT (user_id, keyword_id) DO UPDATE SET
            times = CASE WHEN u.category_id = EXCLUDED.category_id THEN u.times + 1 ELSE 1 END,
            category_id = EXCLUDED.category_id,
            updated_at = now()
        """,
        [(user_id, k, category_id) for k in keyword_ids],
    )


async def delete_keyword_choices(conn: asyncpg.Connection, *, user_id: UUID, keyword_ids: list[UUID]) -> None:
    """Back on the list's category (the user filed it the list's way)."""
    if keyword_ids:
        await conn.execute(
            "DELETE FROM user_keyword_choices WHERE user_id = $1 AND keyword_id = ANY($2::uuid[])",
            user_id, keyword_ids,
        )


async def get_keyword_choice_rows(conn: asyncpg.Connection, *, user_id: UUID, keyword_ids: list[UUID]) -> list[dict]:
    """For an apply's undo snapshot."""
    if not keyword_ids:
        return []
    rows = await conn.fetch(
        """
        SELECT keyword_id, category_id, times FROM user_keyword_choices
        WHERE user_id = $1 AND keyword_id = ANY($2::uuid[])
        """,
        user_id, keyword_ids,
    )
    return [dict(r) for r in rows]


async def restore_keyword_choices(
    conn: asyncpg.Connection, *, user_id: UUID, keyword_ids: list[UUID], choices: list[dict]
) -> None:
    """Puts the user's choices for these keywords back as snapshotted. A
    keyword or category deleted since is skipped rather than failing."""
    if not keyword_ids:
        return
    await delete_keyword_choices(conn, user_id=user_id, keyword_ids=keyword_ids)
    if choices:
        await conn.executemany(
            """
            INSERT INTO user_keyword_choices (user_id, keyword_id, category_id, times)
            SELECT $1, k.id, c.id, $4
            FROM description_keywords k, categories c
            WHERE k.id = $2 AND c.id = $3 AND c.user_id = $1
            """,
            [(user_id, UUID(str(c["keyword_id"])), UUID(str(c["category_id"])), int(c["times"])) for c in choices],
        )


# --- admin (optional cleanup; no approval gate) -------------------------------


async def list_keywords(
    conn: asyncpg.Pool, *, page: int, page_size: int, search: str | None, direction: str | None,
    status: str | None,
) -> tuple[list[dict], int]:
    where, args = [], []
    if search:
        args.append(f"%{search}%")
        where.append(f"(k.pattern ILIKE ${len(args)} OR k.category_name ILIKE ${len(args)})")
    if direction:
        args.append(direction)
        where.append(f"k.direction = ${len(args)}")
    if status:
        args.append(status)
        where.append(f"k.status = ${len(args)}")
    clause = f"WHERE {' AND '.join(where)}" if where else ""
    total = await conn.fetchval(f"SELECT count(*) FROM description_keywords k {clause}", *args)
    args += [page_size, (page - 1) * page_size]
    rows = await conn.fetch(
        f"""
        SELECT {', '.join('k.' + c.strip() for c in _KEYWORD_COLUMNS.split(','))},
               (SELECT count(DISTINCT u.user_id) FROM user_keyword_choices u WHERE u.keyword_id = k.id)::int
                   AS users_choosing_otherwise
        FROM description_keywords k {clause}
        ORDER BY upper(k.pattern), k.direction
        LIMIT ${len(args) - 1} OFFSET ${len(args)}
        """,
        *args,
    )
    return [dict(r) for r in rows], total


async def get_keyword(conn: asyncpg.Connection | asyncpg.Pool, *, keyword_id: UUID) -> dict | None:
    row = await conn.fetchrow(
        f"""
        SELECT {_KEYWORD_COLUMNS},
               (SELECT count(DISTINCT u.user_id) FROM user_keyword_choices u WHERE u.keyword_id = $1)::int
                   AS users_choosing_otherwise
        FROM description_keywords WHERE id = $1
        """,
        keyword_id,
    )
    return dict(row) if row else None


async def create_keyword(
    conn: asyncpg.Connection | asyncpg.Pool, *, admin_id: UUID, pattern: str, direction: str, category_name: str
) -> UUID:
    return await conn.fetchval(
        """
        INSERT INTO description_keywords (pattern, direction, category_name, source, created_by)
        VALUES ($1, $2, $3, 'admin', $4) RETURNING id
        """,
        pattern, direction, category_name, admin_id,
    )


async def update_keyword(
    conn: asyncpg.Connection | asyncpg.Pool, *, keyword_id: UUID, pattern: str | None, category_name: str | None,
    status: str | None,
) -> bool:
    result = await conn.execute(
        """
        UPDATE description_keywords SET
            pattern = COALESCE($2, pattern),
            category_name = COALESCE($3, category_name),
            status = COALESCE($4, status),
            updated_at = now()
        WHERE id = $1
        """,
        keyword_id, pattern, category_name, status,
    )
    return result.endswith(" 1")
