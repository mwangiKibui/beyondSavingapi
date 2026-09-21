from uuid import UUID

import asyncpg


class DuplicateSubLedger(Exception):
    pass


async def create_sub_ledger(
    pool: asyncpg.Pool, *, account_id: UUID, name: str, balance_treatment: str
) -> dict:
    try:
        row = await pool.fetchrow(
            """
            INSERT INTO sub_ledgers (account_id, name, balance_treatment)
            VALUES ($1, $2, $3)
            RETURNING id, account_id, name, balance_treatment, created_at, updated_at
            """,
            account_id,
            name,
            balance_treatment,
        )
    except asyncpg.UniqueViolationError as exc:
        raise DuplicateSubLedger(name) from exc
    return dict(row)


async def list_sub_ledgers(pool: asyncpg.Pool, *, account_id: UUID) -> list[dict]:
    rows = await pool.fetch(
        """
        SELECT id, account_id, name, balance_treatment, created_at, updated_at
        FROM sub_ledgers
        WHERE account_id = $1
        ORDER BY created_at
        """,
        account_id,
    )
    return [dict(row) for row in rows]


async def update_sub_ledger(
    pool: asyncpg.Pool,
    *,
    sub_ledger_id: UUID,
    account_id: UUID,
    name: str | None,
    balance_treatment: str | None,
) -> dict | None:
    # Column names below are hardcoded, not user input - only the values are
    # parameterized - so building the SET clause per which fields were
    # actually given is safe (matches app/repositories/accounts.py's
    # update_account convention).
    set_clauses = ["updated_at = now()"]
    values: list[str] = []

    if name is not None:
        values.append(name)
        set_clauses.append(f"name = ${len(values)}")
    if balance_treatment is not None:
        values.append(balance_treatment)
        set_clauses.append(f"balance_treatment = ${len(values)}")

    values.append(str(sub_ledger_id))
    values.append(str(account_id))

    query = f"""
        UPDATE sub_ledgers
        SET {", ".join(set_clauses)}
        WHERE id = ${len(values) - 1} AND account_id = ${len(values)}
        RETURNING id, account_id, name, balance_treatment, created_at, updated_at
    """

    try:
        row = await pool.fetchrow(query, *values)
    except asyncpg.UniqueViolationError as exc:
        raise DuplicateSubLedger() from exc

    return dict(row) if row else None


async def delete_sub_ledger(pool: asyncpg.Pool, *, sub_ledger_id: UUID, account_id: UUID) -> bool:
    result = await pool.execute(
        "DELETE FROM sub_ledgers WHERE id = $1 AND account_id = $2", sub_ledger_id, account_id
    )
    return result != "DELETE 0"
