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


async def get_transaction(pool: asyncpg.Pool, *, transaction_id: UUID, user_id: UUID) -> dict | None:
    """Single-transaction detail (ab-52), scoped to the owning user via the
    same accounts join as get_account. Status derivation (ab-61) sums each
    allocation's original_amount (the transaction's own currency - see
    get_allocations' own comment on why original, not the budget-converted
    amount) against the transaction's amount: 'reconciled' once it's fully
    covered, 'partial' once some but not all of it is, 'unreconciled' with
    nothing allocated yet. NUMERIC arithmetic in Postgres is exact decimal,
    so no floating-point epsilon is needed here (unlike the equivalent
    dummy/local check on the frontend, which sums IEEE-754 floats).
    """
    row = await pool.fetchrow(
        """
        SELECT
            t.id, t.account_id, t.txn_date, t.amount, t.currency, t.direction,
            t.counterparty, t.description, t.balance_after, t.import_id,
            t.sub_ledger_id, sl.name AS sub_ledger_name,
            CASE
                WHEN COALESCE(alloc.total, 0) >= t.amount THEN 'reconciled'
                WHEN COALESCE(alloc.total, 0) > 0 THEN 'partial'
                ELSE 'unreconciled'
            END AS status
        FROM transactions t
        JOIN accounts a ON a.id = t.account_id
        LEFT JOIN sub_ledgers sl ON sl.id = t.sub_ledger_id
        LEFT JOIN LATERAL (
            SELECT SUM(al.original_amount) AS total FROM allocations al WHERE al.transaction_id = t.id
        ) alloc ON true
        WHERE t.id = $1 AND a.user_id = $2
        """,
        transaction_id,
        user_id,
    )
    return dict(row) if row else None


async def get_allocations(pool: asyncpg.Pool, *, transaction_id: UUID) -> list[dict]:
    """A transaction's allocation breakdown (ab-52) - each split's category
    (LEFT JOIN, since category_id is nullable per ab-124: a null-category
    allocation is a deliberate "reconciled, no category" row, not a
    missing one), amount, and optional note. Caller must already have
    confirmed transaction ownership (see get_transaction).

    Includes both `amount`/`currency` (budget-converted, per
    product-brief.html's "Reconciliation vs. budget math use different
    amounts" decision) and `original_amount`/`original_currency` (the
    transaction's own currency, ab-130) plus `created_at` (ab-130), for
    callers like the reconciliation-history view that need the
    transaction-currency figure and when each split was made.
    """
    rows = await pool.fetch(
        """
        SELECT al.id, al.category_id, c.name AS category_name, al.amount, al.currency,
               al.original_amount, al.original_currency, al.note, al.created_at
        FROM allocations al
        LEFT JOIN categories c ON c.id = al.category_id
        WHERE al.transaction_id = $1
        ORDER BY al.created_at
        """,
        transaction_id,
    )
    return [dict(row) for row in rows]


async def insert_allocations(
    pool: asyncpg.Pool,
    *,
    transaction_id: UUID,
    category_ids: list[UUID | None],
    amounts: list[float],
    currency: str,
    notes: list[str | None],
) -> None:
    """Creates one or more allocation splits for a transaction (ab-57) -
    each a full row (category_id nullable per ab-124 for a "reconciled, no
    category" split), original_amount/original_currency always the
    transaction's own currency. amount/currency (the budget-converted
    figure) are set equal to original for now, fx_rate left at its column
    default of 1 - no FX conversion or budget-plan routing exists yet
    (those are separate, not-yet-built BE tickets). Caller must already
    have validated ownership, the over-allocation guard (ab-59), and the
    category-type-vs-direction guard (ab-60).
    """
    await pool.execute(
        """
        INSERT INTO allocations (transaction_id, category_id, original_amount, original_currency, amount, currency, note)
        SELECT $1, category_id, amount, $4, amount, $4, note
        FROM UNNEST($2::uuid[], $3::numeric[], $5::text[]) AS t(category_id, amount, note)
        """,
        transaction_id,
        category_ids,
        amounts,
        currency,
        notes,
    )


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
    # allocated (via the LATERAL join) is computed once and reused both for
    # the status column and the status_filter branch below - see
    # get_transaction's own comment on the reconciled/partial/unreconciled
    # derivation (ab-61) and why no float epsilon is needed here.
    base_query = """
        WITH transaction_data AS (
            SELECT
                t.id, t.account_id, t.txn_date, t.amount, t.currency, t.direction,
                t.counterparty, t.description, t.balance_after, t.import_id,
                t.sub_ledger_id, sl.name AS sub_ledger_name,
                CASE
                    WHEN COALESCE(alloc.total, 0) >= t.amount THEN 'reconciled'
                    WHEN COALESCE(alloc.total, 0) > 0 THEN 'partial'
                    ELSE 'unreconciled'
                END AS status
            FROM transactions t
            JOIN accounts a ON a.id = t.account_id
            LEFT JOIN sub_ledgers sl ON sl.id = t.sub_ledger_id
            LEFT JOIN LATERAL (
                SELECT SUM(al.original_amount) AS total FROM allocations al WHERE al.transaction_id = t.id
            ) alloc ON true
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
                    OR ($6 = 'reconciled' AND COALESCE(alloc.total, 0) >= t.amount)
                    OR ($6 = 'partial' AND COALESCE(alloc.total, 0) > 0 AND COALESCE(alloc.total, 0) < t.amount)
                    OR ($6 = 'unreconciled' AND COALESCE(alloc.total, 0) = 0)
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
