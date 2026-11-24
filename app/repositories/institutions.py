"""Institutions (ab-184's table): the banks, mobile money services, SACCOs
and money market funds users hold money with. Admin-managed (ab-197/198/
199); never deleted, only retired.

accounts.provider stores the institution's *name* (ab-294 kept it that
way - no foreign key), so a rename here also renames it on accounts, and
the account forms check a provider against this table by name + type.
"""

import re
from uuid import UUID

from app.repositories.statement_profiles import RECOGNISED_EXPR

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



# --- ab-294: account pickers and the CSV import ---------------------------


async def provider_allowed(conn, *, name: str, type: str, current: str | None = None) -> bool:
    """True when an active institution of this type has exactly this name,
    or when it is the account's current provider (so an account whose
    provider was retired can still be edited). `conn` may be a pool."""
    if current is not None and name == current:
        return True
    return bool(await conn.fetchval(
        "SELECT EXISTS (SELECT 1 FROM institutions WHERE name = $1 AND type = $2 AND retired_at IS NULL)",
        name, type,
    ))


async def list_active(conn, *, type: str | None) -> list[dict]:
    """Active institutions for the account pickers, A-Z."""
    rows = await conn.fetch(
        f"""
        SELECT i.id, i.name, i.type, i.statements_uploadable, {RECOGNISED_EXPR}
        FROM institutions i
        WHERE i.retired_at IS NULL AND ($1::text IS NULL OR i.type = $1)
        ORDER BY lower(i.name)
        """,
        type,
    )
    return [dict(r) for r in rows]


async def existing_normalized(conn, *, keys: set[tuple[str, str]]) -> dict[tuple[str, str], bool]:
    """Which (normalized name, type) keys are already taken, retired ones
    included: {key: is_retired}. One query for the whole import file."""
    if not keys:
        return {}
    names = [k[0] for k in keys]
    types = [k[1] for k in keys]
    rows = await conn.fetch(
        f"""
        SELECT {_NORMALIZED_SQL} AS key_name, type, retired_at IS NOT NULL AS retired
        FROM institutions
        WHERE ({_NORMALIZED_SQL}, type) IN (SELECT * FROM unnest($1::text[], $2::text[]))
        """,
        names, types,
    )
    return {(r["key_name"], r["type"]): r["retired"] for r in rows}


async def insert_institutions(conn, *, rows: list[dict]) -> int:
    """Adds the import's new rows (uploads off); a row someone else added in
    the meantime is skipped by the unique index. Returns how many went in."""
    if not rows:
        return 0
    result = await conn.fetch(
        """
        INSERT INTO institutions (name, type, regulator, statements_uploadable)
        SELECT name, type, regulator, false
        FROM unnest($1::text[], $2::text[], $3::text[]) AS t(name, type, regulator)
        ON CONFLICT DO NOTHING
        RETURNING id
        """,
        [r["name"] for r in rows], [r["type"] for r in rows], [r["regulator"] for r in rows],
    )
    return len(result)
