"""Institutions (ab-184's table): the banks, mobile money services, SACCOs
and money market funds users hold money with. Admin-managed (ab-197/198/
199); never deleted, only retired.

accounts.provider still stores the institution's *name* (no foreign key
yet - ab-294 decides), so a rename here also renames it on accounts.
"""

import re
from uuid import UUID

_COLUMNS = "id, name, type, regulator, statements_uploadable, retired_at, created_at, updated_at"

# Same rule as the DB's unique index institutions_name_type_key:
# lower(regexp_replace(name, '\s', '', 'g')).
_NORMALIZED_SQL = "lower(regexp_replace(name, '\\s', '', 'g'))"


def normalize_name(name: str) -> str:
    """Name as the unique index compares it: lower case, no whitespace."""
    return re.sub(r"\s", "", name).lower()


async def list_institutions(
    conn, *, search: str | None, type: str | None, status: str, page: int, page_size: int
) -> tuple[list[dict], int]:
    where, args = [], []
    if search:
        args.append(f"%{search.strip()}%")
        where.append(f"name ILIKE ${len(args)}")
    if type:
        args.append(type)
        where.append(f"type = ${len(args)}")
    if status == "active":
        where.append("retired_at IS NULL")
    elif status == "retired":
        where.append("retired_at IS NOT NULL")
    clause = f"WHERE {' AND '.join(where)}" if where else ""
    args += [page_size, (page - 1) * page_size]
    rows = await conn.fetch(
        f"""
        SELECT {_COLUMNS}, count(*) OVER () AS total
        FROM institutions {clause}
        ORDER BY (retired_at IS NOT NULL), type, lower(name)
        LIMIT ${len(args) - 1} OFFSET ${len(args)}
        """,
        *args,
    )
    total = rows[0]["total"] if rows else 0
    if not rows and page > 1:
        total = await conn.fetchval(f"SELECT count(*) FROM institutions {clause}", *args[:-2])
    return [{k: v for k, v in dict(r).items() if k != "total"} for r in rows], total


async def get_institution(conn, *, institution_id: UUID) -> dict | None:
    row = await conn.fetchrow(f"SELECT {_COLUMNS} FROM institutions WHERE id = $1", institution_id)
    return dict(row) if row else None


async def find_duplicate(conn, *, name: str, type: str, exclude_id: UUID | None) -> dict | None:
    """Another institution of this type whose name matches ignoring case and
    spaces (retired ones included - re-adding means restoring)."""
    row = await conn.fetchrow(
        f"""
        SELECT {_COLUMNS} FROM institutions
        WHERE {_NORMALIZED_SQL} = $1 AND type = $2 AND ($3::uuid IS NULL OR id <> $3)
        LIMIT 1
        """,
        normalize_name(name), type, exclude_id,
    )
    return dict(row) if row else None


async def create_institution(
    conn, *, name: str, type: str, regulator: str | None, statements_uploadable: bool
) -> dict:
    row = await conn.fetchrow(
        f"""
        INSERT INTO institutions (name, type, regulator, statements_uploadable)
        VALUES ($1, $2, $3, $4)
        RETURNING {_COLUMNS}
        """,
        name, type, regulator, statements_uploadable,
    )
    return dict(row)


_UPDATABLE = ("name", "type", "regulator", "statements_uploadable")


async def update_institution(conn, *, institution_id: UUID, changes: dict) -> dict:
    sets, args = [], [institution_id]
    for field in _UPDATABLE:
        if field in changes:
            args.append(changes[field])
            sets.append(f"{field} = ${len(args)}")
    row = await conn.fetchrow(
        f"""
        UPDATE institutions SET {', '.join(sets)}, updated_at = now()
        WHERE id = $1
        RETURNING {_COLUMNS}
        """,
        *args,
    )
    return dict(row)


async def count_accounts_using(conn, *, name: str, type: str) -> int:
    return await conn.fetchval(
        "SELECT count(*) FROM accounts WHERE provider = $1 AND account_type = $2", name, type
    )


async def rename_account_providers(conn, *, old: str, new: str, type: str) -> int:
    """Keeps existing accounts linked by name after a rename."""
    result = await conn.execute(
        "UPDATE accounts SET provider = $2, updated_at = now() WHERE provider = $1 AND account_type = $3",
        old, new, type,
    )
    return int(result.split()[-1])


async def set_retired(conn, *, institution_id: UUID, retired: bool) -> dict:
    row = await conn.fetchrow(
        f"""
        UPDATE institutions
        SET retired_at = CASE WHEN $2 THEN COALESCE(retired_at, now()) ELSE NULL END, updated_at = now()
        WHERE id = $1
        RETURNING {_COLUMNS}
        """,
        institution_id, retired,
    )
    return dict(row)

