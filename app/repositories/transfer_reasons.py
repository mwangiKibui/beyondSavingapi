from uuid import UUID

import asyncpg


class DuplicateTransferReason(Exception):
    pass


_SORT_COLUMNS = {
    "name": "name",
}

# Seeded for every new user on signup (ab-133) - covers the concrete Sacco
# scenarios already identified (loan repayment, share/savings contribution),
# plus a general catch-all for money sent to another of the user's own
# accounts. Users add their own from here, same as categories.
# (name, direction): which way the money moves - None for either way.
# The money-in ones are ab-285 (existing users got them in migration 000028).
DEFAULT_TRANSFER_REASONS: list[tuple[str, str | None]] = [
    ("Loan Repayment", "out"),
    ("Share Contribution", "out"),
    ("Savings Contribution", "out"),
    ("Sent to another of my accounts", "out"),
    ("From my savings", "in"),
    ("From an investment / MMF", "in"),
    ("From family", "in"),
    # ab-288: when no reason fits; the row's note says what it was.
    # Existing users get it via beyondSavingdb migration 000031.
    ("Other", None),
]
# What an approved transfer suggestion files under when it names no other
# reason; also what import handling uses (row_handling.BETWEEN_MY_ACCOUNTS).
BETWEEN_MY_ACCOUNTS = "Between my accounts"


async def seed_default_transfer_reasons(conn: asyncpg.Connection, *, user_id: UUID) -> None:
    await conn.executemany(
        "INSERT INTO transfer_reasons (user_id, name, is_default, direction) VALUES ($1, $2, TRUE, $3)",
        [(user_id, name, direction) for name, direction in DEFAULT_TRANSFER_REASONS],
    )


async def get_user_transfer_reasons(conn, *, user_id: UUID) -> list[dict]:
    """The user's reasons with their direction, for suggestions (ab-285)."""
    rows = await conn.fetch("SELECT id, name, direction FROM transfer_reasons WHERE user_id = $1", user_id)
    return [dict(r) for r in rows]


async def get_transfer_reasons_by_ids(
    pool: asyncpg.Pool, *, transfer_reason_ids: list[UUID], user_id: UUID
) -> list[dict]:
    """Looks up transfer reasons by id, scoped to the owning user - used by
    Create-allocations (ab-134) to confirm every transfer_reason_id
    referenced by a split both exists and belongs to this user. A missing
    id is simply absent from the result, same convention as
    get_categories_by_ids.
    """
    if not transfer_reason_ids:
        return []
    rows = await pool.fetch(
        "SELECT id, name, direction FROM transfer_reasons WHERE id = ANY($1::uuid[]) AND user_id = $2",
        transfer_reason_ids,
        user_id,
    )
    return [dict(row) for row in rows]


async def create_transfer_reason(pool: asyncpg.Pool, *, user_id: UUID, name: str) -> dict:
    try:
        row = await pool.fetchrow(
            """
            INSERT INTO transfer_reasons (user_id, name)
            VALUES ($1, $2)
            RETURNING id, name, is_default, direction, created_at
            """,
            user_id,
            name,
        )
    except asyncpg.UniqueViolationError as exc:
        raise DuplicateTransferReason() from exc
    return dict(row)


async def list_transfer_reasons(
    pool: asyncpg.Pool,
    *,
    user_id: UUID,
    page: int,
    page_size: int,
    search: str | None,
    sort_by: str,
    sort_dir: str,
) -> tuple[list[dict], int]:
    base_query = """
        SELECT id, name, is_default, direction
        FROM transfer_reasons
        WHERE user_id = $1
          AND ($2::text IS NULL OR name ILIKE '%' || $2 || '%')
    """
    params = [user_id, search]

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
