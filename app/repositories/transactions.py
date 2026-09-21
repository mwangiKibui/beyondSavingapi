from datetime import date
from uuid import UUID

import asyncpg

from app.parsers.base import ParsedTransaction

# Whitelisted (not user-supplied directly - FastAPI/Pydantic Literal already
# constrains sort_by/sort_dir before this is reached, but keeping the SQL
# fragment itself off free-form user input is worth the belt-and-braces,
# matching app/repositories/accounts.py's _SORT_COLUMNS convention).
_SORT_COLUMNS = {
    "txn_date": "txn_date",
    "amount": "amount",
}


async def insert_transactions(
    pool: asyncpg.Pool,
    *,
    account_id: UUID,
    import_id: UUID,
    transactions: list[ParsedTransaction],
    sub_ledger_id: UUID | None = None,
) -> int:
    """Bulk-inserts a parser's output against one account, silently
    skipping any row whose (account_id, dedupe_hash) already exists -
    the case where an overlapping/re-uploaded statement covers a
    transaction this account already has (see docs/schema.sql's
    UNIQUE (account_id, dedupe_hash), and ab-43). Returns the number of
    rows actually inserted (i.e. excluding skipped duplicates).

    `sub_ledger_id` tags every inserted row as belonging to one of the
    account's sub-ledgers (ab-119) - left None for the vast majority of
    accounts, which don't have any.
    """
    if not transactions:
        return 0

    rows = await pool.fetch(
        """
        INSERT INTO transactions (
            account_id, import_id, sub_ledger_id, source, txn_date, amount, currency,
            direction, counterparty, description, balance_after, dedupe_hash
        )
        SELECT $1, $2, $3, 'statement', * FROM UNNEST(
            $4::date[], $5::numeric[], $6::text[], $7::text[],
            $8::text[], $9::text[], $10::numeric[], $11::text[]
        )
        ON CONFLICT (account_id, dedupe_hash) DO NOTHING
        RETURNING id
        """,
        account_id,
        import_id,
        sub_ledger_id,
        [txn["txn_date"] for txn in transactions],
        [txn["amount"] for txn in transactions],
        [txn["currency"] for txn in transactions],
        [txn["direction"] for txn in transactions],
        [txn["counterparty"] for txn in transactions],
        [txn["description"] for txn in transactions],
        [txn["balance_after"] for txn in transactions],
        [txn["dedupe_hash"] for txn in transactions],
    )
    return len(rows)


async def insert_manual_transactions(
    pool: asyncpg.Pool,
    *,
    account_ids: list[UUID],
    sub_ledger_ids: list[UUID | None],
    txn_dates: list[date],
    amounts: list[float],
    currencies: list[str],
    directions: list[str],
    descriptions: list[str | None],
) -> list[dict]:
    """Bulk-inserts manual entries (ab-48) - source='manual', with
    import_id, dedupe_hash, and balance_after all NULL per
    beyondSavingdb's own schema comment on the transactions table.
    Unlike insert_transactions' statement path, manual entries are never
    deduplicated, so there's no ON CONFLICT clause here.
    """
    rows = await pool.fetch(
        """
        INSERT INTO transactions (
            account_id, sub_ledger_id, source, txn_date, amount, currency, direction, description
        )
        SELECT account_id, sub_ledger_id, 'manual', txn_date, amount, currency, direction, description
        FROM UNNEST(
            $1::uuid[], $2::uuid[], $3::date[], $4::numeric[], $5::text[], $6::text[], $7::text[]
        ) AS t(account_id, sub_ledger_id, txn_date, amount, currency, direction, description)
        RETURNING id, account_id, txn_date, amount, currency, direction, counterparty, description,
                  balance_after, import_id, sub_ledger_id
        """,
        account_ids,
        sub_ledger_ids,
        txn_dates,
        amounts,
        currencies,
        directions,
        descriptions,
    )
    return [dict(row) for row in rows]


async def list_transactions(
    pool: asyncpg.Pool,
    *,
    user_id: UUID,
    account_id: UUID | None,
    from_date: date | None,
    to_date: date | None,
    direction: str | None,
    status: str | None,
    import_id: UUID | None,
    search: str | None,
    page: int,
    page_size: int,
    sort_by: str,
    sort_dir: str,
) -> tuple[list[dict], int]:
    # balance_after: returned as-is, genuinely NULL for a manual entry
    # (ab-48) since there's no parsed statement to derive it from. No
    # computed running-balance fallback exists for that case - whoever
    # builds one must group by sub_ledger_id first (see ab-119's note on
    # this ticket - a fallback replayed across a whole sub-ledger
    # account's mixed sequences would be wrong).
    #
    # import_id, when given, is the ONLY filter that applies (besides
    # user ownership) - "show me everything this one upload produced,"
    # not a narrower browse combined with whatever other filters happen
    # to be set - matching ab-49's already-shipped UI behavior against
    # mock data.
    base_query = """
        WITH transaction_data AS (
            SELECT
                t.id, t.account_id, t.txn_date, t.amount, t.currency, t.direction,
                t.counterparty, t.description, t.balance_after, t.import_id,
                t.sub_ledger_id, sl.name AS sub_ledger_name,
                CASE
                    WHEN EXISTS (SELECT 1 FROM allocations al WHERE al.transaction_id = t.id)
                    THEN 'reconciled' ELSE 'unreconciled'
                END AS status
            FROM transactions t
            JOIN accounts a ON a.id = t.account_id
            LEFT JOIN sub_ledgers sl ON sl.id = t.sub_ledger_id
            WHERE a.user_id = $1
              AND (
                ($8::uuid IS NOT NULL AND t.import_id = $8)
                OR (
                  $8::uuid IS NULL
                  AND ($2::uuid IS NULL OR t.account_id = $2)
                  AND ($3::date IS NULL OR t.txn_date >= $3)
                  AND ($4::date IS NULL OR t.txn_date <= $4)
                  AND ($5::text IS NULL OR t.direction = $5)
                  AND (
                    $6::text IS NULL
                    OR ($6 = 'reconciled' AND EXISTS (SELECT 1 FROM allocations al WHERE al.transaction_id = t.id))
                    OR ($6 = 'unreconciled' AND NOT EXISTS (SELECT 1 FROM allocations al WHERE al.transaction_id = t.id))
                  )
                  AND (
                    $7::text IS NULL
                    OR t.description ILIKE '%' || $7 || '%'
                    OR t.counterparty ILIKE '%' || $7 || '%'
                  )
                )
              )
        )
        SELECT * FROM transaction_data
    """
    params = [user_id, account_id, from_date, to_date, direction, status, search, import_id]

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
