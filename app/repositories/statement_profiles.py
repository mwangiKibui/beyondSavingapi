"""ab-312: statement profiles - stored layouts the shared reader runs.

Every write goes through validate_profile(): the JSON column can hold
anything, so the API is what keeps cell values out of it.
"""

import json
from uuid import UUID

import asyncpg

from app.parsers.profiles.schema import validate_profile

# Whether an institution (aliased i) has an active profile - the
# "recognised layout" flag on accounts and providers.
RECOGNISED_EXPR = (
    "EXISTS (SELECT 1 FROM statement_profiles sp WHERE sp.institution_id = i.id AND sp.status = 'active')"
    " AS recognised_layout"
)

_ACTIVE = """
    SELECT sp.id, sp.institution_id, i.name AS institution_name, sp.fingerprint, sp.profile,
           sp.version, sp.usage_count, sp.last_check_passed_at
    FROM statement_profiles sp
    JOIN institutions i ON i.id = sp.institution_id
    WHERE sp.status = 'active' AND i.retired_at IS NULL
"""


def _row(row) -> dict:
    out = dict(row)
    if isinstance(out.get("profile"), str):
        out["profile"] = json.loads(out["profile"])
    return out


async def list_active_profiles(pool: asyncpg.Pool) -> list[dict]:
    """Every active profile of every live institution - matching needs the
    other institutions' too, to notice a file uploaded to the wrong account."""
    rows = await pool.fetch(_ACTIVE)
    return [_row(r) for r in rows]


async def get_profile(pool: asyncpg.Pool, *, profile_id: UUID) -> dict | None:
    row = await pool.fetchrow(
        """
        SELECT id, institution_id, fingerprint, profile, version, source, status,
               usage_count, last_check_passed_at, last_check_failed_at, created_at, updated_at
        FROM statement_profiles WHERE id = $1
        """,
        profile_id,
    )
    return _row(row) if row else None


async def create_profile(
    pool: asyncpg.Pool,
    *,
    institution_id: UUID,
    fingerprint: str,
    profile: dict,
    source: str,
    status: str = "draft",
    created_by: UUID | None = None,
) -> dict:
    """Validates, then stores as the next version for this institution and
    fingerprint. Raises InvalidStatementProfile before touching the DB."""
    clean = validate_profile(profile).model_dump(mode="json")
    row = await pool.fetchrow(
        """
        INSERT INTO statement_profiles (institution_id, fingerprint, profile, version, source, status, created_by)
        VALUES (
            $1, $2, $3::jsonb,
            COALESCE((SELECT MAX(version) FROM statement_profiles WHERE institution_id = $1 AND fingerprint = $2), 0) + 1,
            $4, $5, $6
        )
        RETURNING id, institution_id, fingerprint, profile, version, source, status, usage_count, last_check_passed_at
        """,
        institution_id,
        fingerprint,
        json.dumps(clean),
        source,
        status,
        created_by,
    )
    return _row(row)


async def record_check_passed(pool: asyncpg.Pool, *, profile_id: UUID) -> None:
    """A read with this profile passed its balance check."""
    await pool.execute(
        """
        UPDATE statement_profiles
        SET usage_count = usage_count + 1, last_check_passed_at = now(), updated_at = now()
        WHERE id = $1
        """,
        profile_id,
    )


async def record_check_failed(pool: asyncpg.Pool, *, profile_id: UUID) -> None:
    """ab-317: this profile matched a file but its read failed the check."""
    await pool.execute(
        "UPDATE statement_profiles SET last_check_failed_at = now(), updated_at = now() WHERE id = $1",
        profile_id,
    )


# --- ab-317: admin -------------------------------------------------------------

_ADMIN_FIELDS = """id, institution_id, fingerprint, version, source, status, usage_count,
                   last_check_passed_at, last_check_failed_at, created_at, updated_at"""


async def list_institution_profiles(pool: asyncpg.Pool, *, institution_id: UUID) -> list[dict]:
    """Every profile of one institution, active first, then newest."""
    rows = await pool.fetch(
        f"""
        SELECT {_ADMIN_FIELDS} FROM statement_profiles
        WHERE institution_id = $1
        ORDER BY (status = 'active') DESC, fingerprint, version DESC
        """,
        institution_id,
    )
    return [dict(r) for r in rows]


async def get_profile_for_update(conn, *, profile_id: UUID) -> dict | None:
    row = await conn.fetchrow(
        f"SELECT {_ADMIN_FIELDS}, profile FROM statement_profiles WHERE id = $1 FOR UPDATE", profile_id
    )
    return _row(row) if row else None


async def set_profile_status(conn, *, profile_id: UUID, status: str) -> None:
    await conn.execute(
        "UPDATE statement_profiles SET status = $2, updated_at = now() WHERE id = $1", profile_id, status
    )


async def retire_other_active(conn, *, institution_id: UUID, fingerprint: str, keep_id: UUID) -> list[UUID]:
    """The other active versions of this layout, retired so keep_id is the
    one used (matching prefers a checked profile over a new one)."""
    rows = await conn.fetch(
        """
        UPDATE statement_profiles SET status = 'retired', updated_at = now()
        WHERE institution_id = $1 AND fingerprint = $2 AND id <> $3 AND status = 'active'
        RETURNING id
        """,
        institution_id,
        fingerprint,
        keep_id,
    )
    return [r["id"] for r in rows]


# --- ab-313: built-in profiles -------------------------------------------------


async def find_institution_id(conn, *, name: str, type: str) -> UUID | None:
    return await conn.fetchval(
        "SELECT id FROM institutions WHERE name = $1 AND type = $2 AND retired_at IS NULL", name, type
    )


async def list_builtin_profiles(conn, *, institution_id: UUID) -> list[dict]:
    """Every built-in profile of the institution, retired ones too (ab-317:
    one an admin retired must stay retired)."""
    rows = await conn.fetch(
        """
        SELECT id, fingerprint, profile, status FROM statement_profiles
        WHERE institution_id = $1 AND source = 'builtin'
        """,
        institution_id,
    )
    return [_row(r) for r in rows]


async def retire_profiles(conn, *, profile_ids: list[UUID]) -> None:
    await conn.execute(
        "UPDATE statement_profiles SET status = 'retired', updated_at = now() WHERE id = ANY($1::uuid[])",
        profile_ids,
    )
