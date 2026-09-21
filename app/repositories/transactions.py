from uuid import UUID

import asyncpg

from app.parsers.base import ParsedTransaction


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
