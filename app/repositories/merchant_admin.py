"""Admin cleanup of the shared merchant list (ab-253 PR 3 / ab-255).

There is no approval queue (decision 2026-11-10): users' votes become
suggestions on their own. These are optional tools to fix or extend the
list. Vote data is only ever returned as counts - never who voted.
"""

import json
from uuid import UUID

import asyncpg

_MERCHANT_COLUMNS = """
    m.id, m.display_name, m.category_name, m.direction, m.source, m.status, m.notes,
    m.created_at, m.updated_at,
    COALESCE((
        SELECT json_agg(json_build_object(
                   'key_kind', k.key_kind, 'key_value', k.key_value, 'requires_text', k.requires_text,
                   'added_from', k.added_from)
               ORDER BY k.key_kind, k.key_value)
        FROM merchant_directory_keys k WHERE k.merchant_id = m.id
    ), '[]') AS keys
"""


def _merchant(row: asyncpg.Record) -> dict:
    out = dict(row)
    out["keys"] = json.loads(out["keys"]) if isinstance(out["keys"], str) else out["keys"]
    return out


async def _vote_breakdowns(pool, merchant_ids: list[UUID]) -> dict[UUID, list[dict]]:
    """Per merchant: distinct users per category name (ignoring case), from
    votes on its numbers and on its plain names (display name, and name
    patterns without '*'). A user who voted both a name and a number counts
    once."""
    if not merchant_ids:
        return {}
    rows = await pool.fetch(
        """
        WITH m AS (SELECT unnest($1::uuid[]) AS id),
        name_keys AS (
            SELECT md.id, upper(md.display_name) AS key_value, md.direction
            FROM merchant_directory md JOIN m ON m.id = md.id
            UNION
            SELECT k.merchant_id, k.key_value, k.direction
            FROM merchant_directory_keys k JOIN m ON m.id = k.merchant_id
            WHERE k.key_kind = 'name_pattern' AND position('*' IN k.key_value) = 0
        ),
        number_keys AS (
            SELECT k.merchant_id AS id, k.key_kind, k.key_value, k.direction
            FROM merchant_directory_keys k JOIN m ON m.id = k.merchant_id
            WHERE k.key_kind <> 'name_pattern'
        ),
        votes AS (
            SELECT nk.id, v.user_id, v.category_name
            FROM name_keys nk
            JOIN merchant_directory_votes v ON v.merchant_key = nk.key_value AND v.direction = nk.direction
            UNION
            SELECT nk.id, v.user_id, v.category_name
            FROM number_keys nk
            JOIN unknown_number_votes v
              ON v.number_kind = nk.key_kind AND v.number = nk.key_value AND v.direction = nk.direction
        )
        SELECT id, min(category_name) AS category_name, count(DISTINCT user_id) AS users
        FROM votes
        GROUP BY id, lower(category_name)
        ORDER BY users DESC, category_name
        """,
        merchant_ids,
    )
    out: dict[UUID, list[dict]] = {}
    for r in rows:
        out.setdefault(r["id"], []).append({"category_name": r["category_name"], "users": r["users"]})
    return out


async def list_merchants(
    pool: asyncpg.Pool,
    *,
    page: int,
    page_size: int,
    search: str | None,
    source: str | None,
    direction: str | None,
    sort_dir: str,
) -> tuple[list[dict], int]:
    base = """
        FROM merchant_directory m
        WHERE ($1::text IS NULL OR m.display_name ILIKE '%' || $1 || '%' OR m.category_name ILIKE '%' || $1 || '%'
               OR EXISTS (SELECT 1 FROM merchant_directory_keys k
                          WHERE k.merchant_id = m.id AND k.key_value ILIKE '%' || $1 || '%'))
          AND ($2::text IS NULL OR m.source = $2)
          AND ($3::text IS NULL OR m.direction = $3)
    """
    params = [search, source, direction]
    total = await pool.fetchval(f"SELECT count(*) {base}", *params)
    order = "ASC" if sort_dir == "asc" else "DESC"
    rows = await pool.fetch(
        f"SELECT {_MERCHANT_COLUMNS} {base} ORDER BY lower(m.display_name) {order}, m.direction LIMIT $4 OFFSET $5",
        *params,
        page_size,
        (page - 1) * page_size,
    )
    items = [_merchant(r) for r in rows]
    votes = await _vote_breakdowns(pool, [i["id"] for i in items])
    for item in items:
        item["votes"] = votes.get(item["id"], [])
    return items, total


async def get_merchant(pool: asyncpg.Pool | asyncpg.Connection, *, merchant_id: UUID) -> dict | None:
    row = await pool.fetchrow(f"SELECT {_MERCHANT_COLUMNS} FROM merchant_directory m WHERE m.id = $1", merchant_id)
    if row is None:
        return None
    item = _merchant(row)
    item["votes"] = (await _vote_breakdowns(pool, [merchant_id])).get(merchant_id, [])
    return item


async def get_blocked(pool: asyncpg.Pool | asyncpg.Connection, *, numbers: list[tuple[str, str]]) -> list[dict]:
    if not numbers:
        return []
    rows = await pool.fetch(
        """
        SELECT b.number_kind, b.number, b.belongs_to
        FROM blocked_numbers b
        JOIN unnest($1::text[], $2::text[]) AS n(number_kind, number)
          ON n.number_kind = b.number_kind AND n.number = b.number
        """,
        [kind for kind, _ in numbers],
        [number for _, number in numbers],
    )
    return [dict(r) for r in rows]


async def find_conflicts(
    conn: asyncpg.Connection,
    *,
    display_name: str,
    direction: str,
    keys: list[tuple[str, str]],
    exclude_merchant_id: UUID | None,
) -> list[str]:
    """Human-readable clashes with OTHER merchants: the same name for the
    direction, or a key (kind, value) another merchant already owns."""
    problems = []
    taken = await conn.fetchval(
        """
        SELECT display_name FROM merchant_directory
        WHERE lower(display_name) = lower($1) AND direction = $2 AND ($3::uuid IS NULL OR id <> $3)
        """,
        display_name,
        direction,
        exclude_merchant_id,
    )
    if taken:
        problems.append(f'A merchant named "{taken}" already exists for this direction')
    if keys:
        rows = await conn.fetch(
            """
            SELECT k.key_kind, k.key_value, m.display_name
            FROM merchant_directory_keys k
            JOIN merchant_directory m ON m.id = k.merchant_id
            JOIN unnest($1::text[], $2::text[]) AS n(key_kind, key_value)
              ON n.key_kind = k.key_kind AND n.key_value = k.key_value
            WHERE k.direction = $3 AND ($4::uuid IS NULL OR k.merchant_id <> $4)
            ORDER BY k.key_kind, k.key_value
            """,
            [kind for kind, _ in keys],
            [value for _, value in keys],
            direction,
            exclude_merchant_id,
        )
        for r in rows:
            label = "Pattern" if r["key_kind"] == "name_pattern" else r["key_kind"].capitalize()
            problems.append(f'{label} {r["key_value"]} already belongs to "{r["display_name"]}"')
    return problems


async def _replace_keys(conn: asyncpg.Connection, *, merchant_id: UUID, direction: str, keys: list[dict]) -> None:
    await conn.execute("DELETE FROM merchant_directory_keys WHERE merchant_id = $1", merchant_id)
    if keys:
        await conn.executemany(
            """
            INSERT INTO merchant_directory_keys (merchant_id, direction, key_kind, key_value, requires_text)
            VALUES ($1, $2, $3, $4, $5)
            """,
            [(merchant_id, direction, k["key_kind"], k["key_value"], k["requires_text"]) for k in keys],
        )


async def create_merchant(
    conn: asyncpg.Connection, *, admin_id: UUID, display_name: str, category_name: str, direction: str,
    keys: list[dict],
) -> UUID:
    """A merchant an admin adds by hand: source 'admin', active at once."""
    merchant_id = await conn.fetchval(
        """
        INSERT INTO merchant_directory
            (display_name, category_name, direction, source, status, approved_by, approved_at)
        VALUES ($1, $2, $3, 'admin', 'approved', $4, now())
        RETURNING id
        """,
        display_name,
        category_name,
        direction,
        admin_id,
    )
    await _replace_keys(conn, merchant_id=merchant_id, direction=direction, keys=keys)
    return merchant_id


async def update_merchant(
    conn: asyncpg.Connection, *, merchant_id: UUID, display_name: str, category_name: str, direction: str,
    keys: list[dict] | None,
) -> None:
    await conn.execute(
        "UPDATE merchant_directory SET display_name = $2, category_name = $3, updated_at = now() WHERE id = $1",
        merchant_id,
        display_name,
        category_name,
    )
    if keys is not None:
        await _replace_keys(conn, merchant_id=merchant_id, direction=direction, keys=keys)


async def delete_merchant(pool: asyncpg.Pool, *, merchant_id: UUID) -> bool:
    deleted = await pool.fetchval("DELETE FROM merchant_directory WHERE id = $1 RETURNING id", merchant_id)
    return deleted is not None


async def list_from_users(
    pool: asyncpg.Pool, *, min_users: int, page: int, page_size: int, search: str | None
) -> tuple[list[dict], int]:
    """What users have chosen for business names and numbers, once
    min_users+ distinct users voted - the list growing on its own. Each
    item has its vote breakdown and whether it's already on the shared
    list by that exact name or number."""
    base = """
        WITH groups AS (
            SELECT 'name' AS key_kind, merchant_key AS key_value, direction,
                   count(DISTINCT user_id) AS users
            FROM merchant_directory_votes GROUP BY merchant_key, direction
            UNION ALL
            SELECT number_kind, number, direction, count(DISTINCT user_id)
            FROM unknown_number_votes GROUP BY number_kind, number, direction
        )
        SELECT g.* FROM groups g
        WHERE g.users >= $1 AND ($2::text IS NULL OR g.key_value ILIKE '%' || $2 || '%')
    """
    total = await pool.fetchval(f"SELECT count(*) FROM ({base}) t", min_users, search)
    rows = await pool.fetch(
        f"""
        SELECT t.*,
               EXISTS (
                   SELECT 1 FROM merchant_directory_keys k
                   WHERE k.direction = t.direction
                     AND ((t.key_kind = 'name' AND k.key_kind = 'name_pattern' AND k.key_value = t.key_value)
                          OR (t.key_kind <> 'name' AND k.key_kind = t.key_kind AND k.key_value = t.key_value))
               ) OR EXISTS (
                   SELECT 1 FROM merchant_directory m
                   WHERE t.key_kind = 'name' AND upper(m.display_name) = t.key_value AND m.direction = t.direction
               ) AS on_list
        FROM ({base}) t
        ORDER BY t.users DESC, t.key_value
        LIMIT $3 OFFSET $4
        """,
        min_users,
        search,
        page_size,
        (page - 1) * page_size,
    )
    items = [dict(r) for r in rows]
    await _attach_group_breakdowns(pool, items)
    return items, total


async def _attach_group_breakdowns(pool, items: list[dict]) -> None:
    names = [i for i in items if i["key_kind"] == "name"]
    numbers = [i for i in items if i["key_kind"] != "name"]
    found: dict[tuple, list[dict]] = {}
    if names:
        for r in await pool.fetch(
            """
            SELECT v.merchant_key AS key_value, v.direction, min(v.category_name) AS category_name,
                   count(DISTINCT v.user_id) AS users
            FROM merchant_directory_votes v
            JOIN unnest($1::text[], $2::text[]) AS k(key_value, direction)
              ON k.key_value = v.merchant_key AND k.direction = v.direction
            GROUP BY v.merchant_key, v.direction, lower(v.category_name)
            ORDER BY users DESC, category_name
            """,
            [i["key_value"] for i in names],
            [i["direction"] for i in names],
        ):
            found.setdefault(("name", r["key_value"], r["direction"]), []).append(
                {"category_name": r["category_name"], "users": r["users"]})
    if numbers:
        for r in await pool.fetch(
            """
            SELECT v.number_kind, v.number, v.direction, min(v.category_name) AS category_name,
                   count(DISTINCT v.user_id) AS users
            FROM unknown_number_votes v
            JOIN unnest($1::text[], $2::text[], $3::text[]) AS k(number_kind, number, direction)
              ON k.number_kind = v.number_kind AND k.number = v.number AND k.direction = v.direction
            GROUP BY v.number_kind, v.number, v.direction, lower(v.category_name)
            ORDER BY users DESC, category_name
            """,
            [i["key_kind"] for i in numbers],
            [i["key_value"] for i in numbers],
            [i["direction"] for i in numbers],
        ):
            found.setdefault((r["number_kind"], r["number"], r["direction"]), []).append(
                {"category_name": r["category_name"], "users": r["users"]})
    for item in items:
        item["votes"] = found.get((item["key_kind"], item["key_value"], item["direction"]), [])


async def list_unknown_numbers(
    pool: asyncpg.Pool, *, min_users: int, page: int, page_size: int
) -> tuple[list[dict], int]:
    """Paybill/Till numbers min_users+ distinct users categorised that the
    shared list has no key for - leads for an admin to research. Blocked
    (shared/misattributed) and dismissed numbers are left out."""
    base = """
        SELECT v.number_kind AS key_kind, v.number AS key_value, v.direction,
               count(DISTINCT v.user_id) AS users,
               -- Name other users' M-Pesa statements printed for it (ab-253 PR 4).
               (SELECT s.name FROM number_name_sightings s
                WHERE s.number_kind = v.number_kind AND s.number = v.number AND s.direction = v.direction
                GROUP BY s.name
                ORDER BY count(DISTINCT s.user_id) DESC, max(s.seen_at) DESC
                LIMIT 1) AS learned_name
        FROM unknown_number_votes v
        WHERE NOT EXISTS (SELECT 1 FROM blocked_numbers b
                          WHERE b.number_kind = v.number_kind AND b.number = v.number)
          AND NOT EXISTS (SELECT 1 FROM merchant_directory_keys k
                          WHERE k.key_kind = v.number_kind AND k.key_value = v.number AND k.direction = v.direction)
          AND NOT EXISTS (SELECT 1 FROM dismissed_unknown_numbers d
                          WHERE d.number_kind = v.number_kind AND d.number = v.number AND d.direction = v.direction)
        GROUP BY v.number_kind, v.number, v.direction
        HAVING count(DISTINCT v.user_id) >= $1
    """
    total = await pool.fetchval(f"SELECT count(*) FROM ({base}) t", min_users)
    rows = await pool.fetch(
        f"{base} ORDER BY users DESC, v.number LIMIT $2 OFFSET $3", min_users, page_size, (page - 1) * page_size
    )
    items = [dict(r) for r in rows]
    await _attach_group_breakdowns(pool, items)
    return items, total


async def dismiss_unknown_number(
    pool: asyncpg.Pool, *, admin_id: UUID, number_kind: str, number: str, direction: str
) -> None:
    await pool.execute(
        """
        INSERT INTO dismissed_unknown_numbers (number_kind, number, direction, dismissed_by)
        VALUES ($1, $2, $3, $4)
        ON CONFLICT (number_kind, number, direction) DO NOTHING
        """,
        number_kind,
        number,
        direction,
        admin_id,
    )
