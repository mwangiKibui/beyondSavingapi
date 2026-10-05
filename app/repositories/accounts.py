from datetime import date
from uuid import UUID

import asyncpg


class DuplicateAccount(Exception):
    pass


# Book balance: anchored on the MOST RECENT applicable balance-brought-
# forward figure (a statement's own stated opening balance, persisted on
# statement_imports/statement_import_sub_ledgers - see migration 000012
# in beyondSavingdb) rather than the latest transaction's raw
# balance_after. An older import's opening balance is superseded the
# moment a newer one lands (picking MAX(opening_balance_date) does this
# with no special-casing) since the newer statement's own figure already
# reflects everything that happened in the real account, including
# anything this app never saw. Transactions dated on/after the anchor -
# of ANY source, manual entries included - are replayed forward on top
# of it, which is what lets a manual entry added after an import still
# count. An account with no usable anchor at all (never had a statement
# with a derivable opening balance) degrades to an implicit 0 anchor
# replayed from the beginning, rather than the old "always reads 0"
# behavior regardless of manual history.
#
# Shared between list_accounts, get_currency_summary, and get_book_balance
# (all need the identical per-account balance derivation); `anchor`/
# `direct_net` alias the direct (non-sub-ledger) case, `sub_ledger_balance`
# the ab-119 sub-ledger case (same formula, scoped per sub-ledger via its
# own statement_import_sub_ledgers anchor). `as_of_expr` is a raw SQL
# expression (either the literal CURRENT_DATE for "today", or a bound
# parameter placeholder like "$2" for get_book_balance's own arbitrary
# as_of lookback) - never caller-supplied text, so no injection risk.
def _book_balance_joins(as_of_expr: str) -> str:
    return f"""
    LEFT JOIN LATERAL (
        SELECT si.opening_balance, si.opening_balance_date
        FROM statement_imports si
        WHERE si.account_id = a.id
          AND si.opening_balance IS NOT NULL
          AND si.opening_balance_date <= {as_of_expr}
        ORDER BY si.opening_balance_date DESC, si.created_at DESC
        LIMIT 1
    ) anchor ON true
    LEFT JOIN LATERAL (
        SELECT COALESCE(SUM(CASE WHEN t.direction = 'in' THEN t.amount ELSE -t.amount END), 0) AS net
        FROM transactions t
        WHERE t.account_id = a.id AND t.sub_ledger_id IS NULL
          AND t.txn_date >= COALESCE(anchor.opening_balance_date, '-infinity'::date)
          AND t.txn_date <= {as_of_expr}
    ) direct_net ON true
    LEFT JOIN LATERAL (
        SELECT SUM(
            CASE sl.balance_treatment
                WHEN 'addition' THEN COALESCE(sl_anchor.opening_balance, 0) + sl_net.net
                ELSE -(COALESCE(sl_anchor.opening_balance, 0) + sl_net.net)
            END
        ) AS total
        FROM sub_ledgers sl
        LEFT JOIN LATERAL (
            SELECT sis.opening_balance, sis.opening_balance_date
            FROM statement_import_sub_ledgers sis
            WHERE sis.sub_ledger_id = sl.id
              AND sis.opening_balance IS NOT NULL
              AND sis.opening_balance_date <= {as_of_expr}
            ORDER BY sis.opening_balance_date DESC
            LIMIT 1
        ) sl_anchor ON true
        LEFT JOIN LATERAL (
            SELECT COALESCE(SUM(CASE WHEN t.direction = 'in' THEN t.amount ELSE -t.amount END), 0) AS net
            FROM transactions t
            WHERE t.sub_ledger_id = sl.id
              AND t.txn_date >= COALESCE(sl_anchor.opening_balance_date, '-infinity'::date)
              AND t.txn_date <= {as_of_expr}
        ) sl_net ON true
        WHERE sl.account_id = a.id
    ) sub_ledger_balance ON true
"""


_BOOK_BALANCE_JOINS = _book_balance_joins("CURRENT_DATE")
_BOOK_BALANCE_EXPR = "COALESCE(sub_ledger_balance.total, COALESCE(anchor.opening_balance, 0) + direct_net.net)"


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


async def get_accounts_by_ids(pool: asyncpg.Pool, *, account_ids: list[UUID], user_id: UUID) -> list[dict]:
    """Looks up accounts by id, scoped to the owning user - used by
    Create-allocations (ab-134) to confirm every source_account_id
    referenced by a transfer-shaped split both exists and belongs to this
    user. A missing id is simply absent from the result, same convention
    as get_categories_by_ids.
    """
    if not account_ids:
        return []
    rows = await pool.fetch(
        "SELECT id, nickname FROM accounts WHERE id = ANY($1::uuid[]) AND user_id = $2",
        account_ids,
        user_id,
    )
    return [dict(row) for row in rows]


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
    # balance ("book balance" - see _BOOK_BALANCE_JOINS above): anchored
    # on the latest applicable balance-brought-forward figure and
    # replayed forward, not just the latest transaction's balance_after.
    # unreconciled_count: transactions not yet fully allocated (ab-61) -
    # covers both "partial" and "unreconciled" states, either of which
    # still needs attention at the account level (this filter/count stays
    # two-state - reconciled vs. not - "partial" is a transaction-level
    # distinction, not an account-level one).
    base_query = f"""
        WITH account_data AS (
            SELECT
                a.id, a.nickname, a.account_type, a.provider, a.account_number, a.currency,
                a.is_active,
                {_BOOK_BALANCE_EXPR} AS balance,
                COALESCE(unreconciled.count, 0) AS unreconciled_count
            FROM accounts a
            {_BOOK_BALANCE_JOINS}
            LEFT JOIN LATERAL (
                SELECT COUNT(*) AS count FROM transactions t
                WHERE t.account_id = a.id
                AND COALESCE(
                    (SELECT SUM(al.original_amount) FROM allocations al WHERE al.transaction_id = t.id), 0
                ) < t.amount
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
    # Same book balance derivation as list_accounts (see _BOOK_BALANCE_JOINS
    # above), summed per currency. Unfiltered - a dashboard summary
    # reflects the user's whole account list, not whatever search/filter
    # state the accounts table happens to be in.
    rows = await pool.fetch(
        f"""
        SELECT
            a.currency,
            COUNT(*) AS account_count,
            COALESCE(SUM({_BOOK_BALANCE_EXPR}), 0) AS total
        FROM accounts a
        {_BOOK_BALANCE_JOINS}
        WHERE a.user_id = $1
          AND a.is_active = true
        GROUP BY a.currency
        ORDER BY a.currency
        """,
        user_id,
    )
    return [dict(row) for row in rows]


async def get_book_balance(
    pool: asyncpg.Pool, *, account_id: UUID, user_id: UUID, as_of: date
) -> float | None:
    """Same book balance derivation as list_accounts (see
    _book_balance_joins above), for one account as of an arbitrary date -
    the statement drawer's "as of" lookback (picking an earlier `To` date
    shows the BBF that was in effect back then, not today's). Returns
    None if the account doesn't exist or isn't owned by this user.
    """
    row = await pool.fetchrow(
        f"""
        SELECT {_BOOK_BALANCE_EXPR} AS balance
        FROM accounts a
        {_book_balance_joins("$3")}
        WHERE a.id = $1 AND a.user_id = $2
        """,
        account_id,
        user_id,
        as_of,
    )
    return float(row["balance"]) if row else None
