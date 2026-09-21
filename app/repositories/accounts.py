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


async def get_account(pool: asyncpg.Pool, *, account_id: UUID, user_id: UUID) -> dict | None:
    row = await pool.fetchrow(
        """
        SELECT id, nickname, account_type, provider, account_number, currency,
               is_active, created_at, updated_at
        FROM accounts
        WHERE id = $1 AND user_id = $2
        """,
        account_id,
        user_id,
    )
    return dict(row) if row else None


async def update_account(
    pool: asyncpg.Pool,
    *,
    account_id: UUID,
    user_id: UUID,
    nickname: str | None,
    account_type: str | None,
    provider: str | None,
    account_number: str | None,
) -> dict | None:
    # Column names below are hardcoded, not user input - only the values are
    # parameterized - so building the SET clause per which fields were
    # actually given is safe.
    set_clauses = ["updated_at = now()"]
    values: list[str] = []

    if nickname is not None:
        values.append(nickname)
        set_clauses.append(f"nickname = ${len(values)}")
    if account_type is not None:
        values.append(account_type)
        set_clauses.append(f"account_type = ${len(values)}")
    if provider is not None:
        values.append(provider)
        set_clauses.append(f"provider = ${len(values)}")
    if account_number is not None:
        values.append(account_number)
        set_clauses.append(f"account_number = ${len(values)}")

    values.append(str(account_id))
    values.append(str(user_id))

    query = f"""
        UPDATE accounts
        SET {", ".join(set_clauses)}
        WHERE id = ${len(values) - 1} AND user_id = ${len(values)}
        RETURNING id, nickname, account_type, provider, account_number, currency,
                  is_active, created_at, updated_at
    """

    try:
        row = await pool.fetchrow(query, *values)
    except asyncpg.UniqueViolationError as exc:
        raise DuplicateAccount() from exc

    return dict(row) if row else None


async def deactivate_account(pool: asyncpg.Pool, *, account_id: UUID, user_id: UUID) -> dict | None:
    row = await pool.fetchrow(
        """
        UPDATE accounts
        SET is_active = false, updated_at = now()
        WHERE id = $1 AND user_id = $2
        RETURNING id, nickname, account_type, provider, account_number, currency,
                  is_active, created_at, updated_at
        """,
        account_id,
        user_id,
    )
    return dict(row) if row else None


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
    # balance: for an account with no sub-ledgers (the vast majority),
    # the most recent transaction's balance_after directly, or 0 if it
    # has none yet. For an account WITH sub-ledgers (ab-119), each
    # sub-ledger has its own independent running balance - combined by
    # taking each one's own latest balance_after and summing them per
    # their stored balance_treatment ('addition' adds, 'deduction'
    # subtracts). sub_ledger_balance.total is non-NULL (even when it's
    # exactly 0) whenever the account has ANY sub_ledgers rows, which is
    # what lets the COALESCE below prefer it over the direct-balance
    # fallback only for accounts that actually have sub-ledgers.
    # unreconciled_count: transactions with zero allocation rows - a
    # placeholder proxy until ab-61 owns the real reconciled/partial/
    # unreconciled state machine.
    base_query = """
        WITH account_data AS (
            SELECT
                a.id, a.nickname, a.account_type, a.provider, a.account_number, a.currency,
                a.is_active,
                COALESCE(sub_ledger_balance.total, direct_txn.balance_after, 0) AS balance,
                COALESCE(unreconciled.count, 0) AS unreconciled_count
            FROM accounts a
            LEFT JOIN LATERAL (
                SELECT balance_after FROM transactions t
                WHERE t.account_id = a.id AND t.sub_ledger_id IS NULL
                ORDER BY t.txn_date DESC, t.created_at DESC
                LIMIT 1
            ) direct_txn ON true
            LEFT JOIN LATERAL (
                SELECT SUM(
                    CASE sl.balance_treatment
                        WHEN 'addition' THEN COALESCE(sl_txn.balance_after, 0)
                        ELSE -COALESCE(sl_txn.balance_after, 0)
                    END
                ) AS total
                FROM sub_ledgers sl
                LEFT JOIN LATERAL (
                    SELECT balance_after FROM transactions t
                    WHERE t.sub_ledger_id = sl.id
                    ORDER BY t.txn_date DESC, t.created_at DESC
                    LIMIT 1
                ) sl_txn ON true
                WHERE sl.account_id = a.id
            ) sub_ledger_balance ON true
            LEFT JOIN LATERAL (
                SELECT COUNT(*) AS count FROM transactions t
                WHERE t.account_id = a.id
                AND NOT EXISTS (SELECT 1 FROM allocations al WHERE al.transaction_id = t.id)
            ) unreconciled ON true
            WHERE a.user_id = $1
              AND a.is_active = true
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
    # Same balance derivation as list_accounts (see its own comment for
    # the sub-ledger rollup logic), summed per currency. Unfiltered - a
    # dashboard summary reflects the user's whole account list, not
    # whatever search/filter state the accounts table happens to be in.
    rows = await pool.fetch(
        """
        SELECT
            a.currency,
            COUNT(*) AS account_count,
            COALESCE(SUM(COALESCE(sub_ledger_balance.total, direct_txn.balance_after, 0)), 0) AS total
        FROM accounts a
        LEFT JOIN LATERAL (
            SELECT balance_after FROM transactions t
            WHERE t.account_id = a.id AND t.sub_ledger_id IS NULL
            ORDER BY t.txn_date DESC, t.created_at DESC
            LIMIT 1
        ) direct_txn ON true
        LEFT JOIN LATERAL (
            SELECT SUM(
                CASE sl.balance_treatment
                    WHEN 'addition' THEN COALESCE(sl_txn.balance_after, 0)
                    ELSE -COALESCE(sl_txn.balance_after, 0)
                END
            ) AS total
            FROM sub_ledgers sl
            LEFT JOIN LATERAL (
                SELECT balance_after FROM transactions t
                WHERE t.sub_ledger_id = sl.id
                ORDER BY t.txn_date DESC, t.created_at DESC
                LIMIT 1
            ) sl_txn ON true
            WHERE sl.account_id = a.id
        ) sub_ledger_balance ON true
        WHERE a.user_id = $1
          AND a.is_active = true
        GROUP BY a.currency
        ORDER BY a.currency
        """,
        user_id,
    )
    return [dict(row) for row in rows]
