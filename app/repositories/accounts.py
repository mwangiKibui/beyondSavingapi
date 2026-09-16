from uuid import UUID

import asyncpg


class DuplicateAccount(Exception):
    pass


# Whitelisted (not user-supplied directly - FastAPI/Pydantic Literal already
# constrains sort_by/sort_dir before this is reached, but keeping the SQL
# fragment itself off free-form user input is worth the belt-and-braces).
# Plain column names, not "a.<col>" - the outer query sorts the CTE's
# result (account_data), where the "a" accounts-table alias isn't in scope.
_SORT_COLUMNS = {
    "nickname": "nickname",
    "provider": "provider",
    "currency": "currency",
    "balance": "balance",
    "unreconciled_count": "unreconciled_count",
}


async def create_account(
    pool: asyncpg.Pool,
    *,
    user_id: UUID,
    nickname: str,
    account_type: str,
    provider: str,
    account_number: str,
    currency: str,
) -> dict:
    try:
        row = await pool.fetchrow(
            """
            INSERT INTO accounts (user_id, nickname, account_type, provider, account_number, currency)
            VALUES ($1, $2, $3, $4, $5, $6)
            RETURNING id, nickname, account_type, provider, account_number, currency,
                      is_active, created_at, updated_at
            """,
            user_id,
            nickname,
            account_type,
            provider,
            account_number,
            currency,
        )
    except asyncpg.UniqueViolationError as exc:
        raise DuplicateAccount(account_number) from exc
    return dict(row)


async def list_accounts(
    pool: asyncpg.Pool,
    *,
    user_id: UUID,
    page: int,
    page_size: int,
    search: str | None,
    sort_by: str,
    sort_dir: str,
    account_type: str | None,
    currency: str | None,
    reconciliation_status: str | None,
) -> tuple[list[dict], int]:
    # balance: the most recent transaction's balance_after for the account,
    # or 0 if it has none yet (no create-transaction API exists yet, ab-47/48).
    # unreconciled_count: transactions with zero allocation rows - a
    # placeholder proxy until ab-61 owns the real reconciled/partial/
    # unreconciled state machine.
    base_query = """
        WITH account_data AS (
            SELECT
                a.id, a.nickname, a.account_type, a.provider, a.account_number, a.currency,
                COALESCE(latest_txn.balance_after, 0) AS balance,
                COALESCE(unreconciled.count, 0) AS unreconciled_count
            FROM accounts a
            LEFT JOIN LATERAL (
                SELECT balance_after FROM transactions t
                WHERE t.account_id = a.id
                ORDER BY t.txn_date DESC, t.created_at DESC
                LIMIT 1
            ) latest_txn ON true
            LEFT JOIN LATERAL (
                SELECT COUNT(*) AS count FROM transactions t
                WHERE t.account_id = a.id
                AND NOT EXISTS (SELECT 1 FROM allocations al WHERE al.transaction_id = t.id)
            ) unreconciled ON true
            WHERE a.user_id = $1
              AND ($2::text IS NULL OR a.nickname ILIKE '%' || $2 || '%')
              AND ($3::text IS NULL OR a.account_type = $3)
              AND ($4::text IS NULL OR a.currency = $4)
        )
        SELECT * FROM account_data
        WHERE
            $5::text IS NULL
            OR ($5 = 'reconciled' AND unreconciled_count = 0)
            OR ($5 = 'unreconciled' AND unreconciled_count > 0)
    """
    params = [user_id, search, account_type, currency, reconciliation_status]

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
    rows = await pool.fetch(
        paged_query, *params, page_size, (page - 1) * page_size
    )

    return [dict(row) for row in rows], total


async def get_currency_summary(pool: asyncpg.Pool, *, user_id: UUID) -> list[dict]:
    # Same balance derivation as list_accounts (most recent transaction's
    # balance_after, 0 if none), summed per currency. Unfiltered - a
    # dashboard summary reflects the user's whole account list, not
    # whatever search/filter state the accounts table happens to be in.
    rows = await pool.fetch(
        """
        SELECT
            a.currency,
            COUNT(*) AS account_count,
            COALESCE(SUM(COALESCE(latest_txn.balance_after, 0)), 0) AS total
        FROM accounts a
        LEFT JOIN LATERAL (
            SELECT balance_after FROM transactions t
            WHERE t.account_id = a.id
            ORDER BY t.txn_date DESC, t.created_at DESC
            LIMIT 1
        ) latest_txn ON true
        WHERE a.user_id = $1
        GROUP BY a.currency
        ORDER BY a.currency
        """,
        user_id,
    )
    return [dict(row) for row in rows]
