"""SQL for rows handled at import (ab-281). Every write takes the
connection the caller has opened a transaction on."""

from datetime import date
from uuid import UUID

import asyncpg


async def get_owned_providers(conn: asyncpg.Connection, *, user_id: UUID) -> set[str]:
    rows = await conn.fetch("SELECT DISTINCT provider FROM accounts WHERE user_id = $1", user_id)
    return {row["provider"] for row in rows}


async def lock_import(conn: asyncpg.Connection, *, import_id: UUID) -> dict | None:
    """Locks the import row so two requests can't run its handling at once,
    and returns the fresh handled_at."""
    row = await conn.fetchrow("SELECT handled_at FROM statement_imports WHERE id = $1 FOR UPDATE", import_id)
    return dict(row) if row else None


async def mark_import_handled(conn: asyncpg.Connection, *, import_id: UUID) -> None:
    await conn.execute("UPDATE statement_imports SET handled_at = now() WHERE id = $1", import_id)


async def get_or_create_transfer_reason(conn: asyncpg.Connection, *, user_id: UUID, name: str) -> UUID:
    return await conn.fetchval(
        """
        INSERT INTO transfer_reasons (user_id, name, is_default) VALUES ($1, $2, TRUE)
        ON CONFLICT (user_id, lower(name)) DO UPDATE SET name = transfer_reasons.name
        RETURNING id
        """,
        user_id,
        name,
    )


async def set_handled(
    conn: asyncpg.Connection, *, transaction_ids: list[UUID], kinds: list[str | None], reasons: list[str | None]
) -> None:
    await conn.execute(
        """
        UPDATE transactions t SET handled_as = h.kind, handled_reason = h.reason, handling_accepted_at = NULL
        FROM UNNEST($1::uuid[], $2::text[], $3::text[]) AS h(id, kind, reason)
        WHERE t.id = h.id
        """,
        transaction_ids,
        kinds,
        reasons,
    )


async def insert_handled_allocations(conn: asyncpg.Connection, *, allocations: list[dict]) -> None:
    """One allocation per handled row: a category (charges, loan
    repayments), none (other loan rows - reconciled, no category) or a
    transfer reason with its source (the other account, or the other
    side's provider name while it waits for its pair)."""
    if not allocations:
        return
    await conn.execute(
        """
        INSERT INTO allocations (
            transaction_id, category_id, transfer_reason_id, source_account_id, source_description,
            original_amount, original_currency, amount, currency
        )
        SELECT transaction_id, category_id, transfer_reason_id, source_account_id, source_description,
               amount, currency, amount, currency
        FROM UNNEST($1::uuid[], $2::uuid[], $3::uuid[], $4::uuid[], $5::text[], $6::numeric[], $7::text[])
            AS a(transaction_id, category_id, transfer_reason_id, source_account_id, source_description, amount, currency)
        """,
        [a["transaction_id"] for a in allocations],
        [a.get("category_id") for a in allocations],
        [a.get("transfer_reason_id") for a in allocations],
        [a.get("source_account_id") for a in allocations],
        [a.get("source_description") for a in allocations],
        [a["amount"] for a in allocations],
        [a["currency"] for a in allocations],
    )


async def delete_allocations(conn: asyncpg.Connection, *, transaction_ids: list[UUID]) -> None:
    await conn.execute("DELETE FROM allocations WHERE transaction_id = ANY($1::uuid[])", transaction_ids)


async def get_waiting_transfer_legs(conn: asyncpg.Connection, *, user_id: UUID) -> list[dict]:
    rows = await conn.fetch(
        """
        SELECT t.id, t.account_id, a.provider, a.nickname, t.direction, t.amount, t.currency,
               t.txn_date, t.description, t.handled_as, sl.role AS sub_ledger_role
        FROM transactions t
        JOIN accounts a ON a.id = t.account_id
        LEFT JOIN sub_ledgers sl ON sl.id = t.sub_ledger_id
        WHERE a.user_id = $1 AND t.handled_as = 'transfer' AND t.transfer_group_id IS NULL
        ORDER BY t.txn_date, t.id
        """,
        user_id,
    )
    return [dict(row) for row in rows]


async def get_pair_candidates(
    conn: asyncpg.Connection, *, user_id: UUID, amounts: list[float], from_date: date, to_date: date
) -> list[dict]:
    """Rows that could be the other leg of a waiting transfer: on the
    user's accounts, unpaired, never dismissed, and either a waiting leg
    or a row nobody has handled or categorised. Locked, so a concurrent
    pairing can't claim the same row."""
    rows = await conn.fetch(
        """
        SELECT t.id, t.account_id, a.provider, a.nickname, t.direction, t.amount, t.currency,
               t.txn_date, t.description, t.handled_as, sl.role AS sub_ledger_role
        FROM transactions t
        JOIN accounts a ON a.id = t.account_id
        LEFT JOIN sub_ledgers sl ON sl.id = t.sub_ledger_id
        WHERE a.user_id = $1
          AND t.amount = ANY($2::numeric[])
          AND t.txn_date BETWEEN $3 AND $4
          AND t.transfer_group_id IS NULL
          AND NOT t.handling_dismissed
          AND (
                t.handled_as = 'transfer'
             OR (t.handled_as IS NULL AND NOT EXISTS (SELECT 1 FROM allocations al WHERE al.transaction_id = t.id))
             OR (t.handled_as = 'saving' AND sl.role IN ('locked_saving', 'shares'))
          )
        ORDER BY t.id
        FOR UPDATE OF t
        """,
        user_id,
        amounts,
        from_date,
        to_date,
    )
    return [dict(row) for row in rows]


async def link_transfer_pair(
    conn: asyncpg.Connection, *, group_id: UUID, ids: list[UUID], reasons: list[str]
) -> None:
    await conn.execute(
        """
        UPDATE transactions t
        SET transfer_group_id = $1, handled_as = 'transfer', handled_reason = p.reason
        FROM UNNEST($2::uuid[], $3::text[]) AS p(id, reason)
        WHERE t.id = p.id
        """,
        group_id,
        ids,
        reasons,
    )


async def set_transfer_group(conn: asyncpg.Connection, *, group_id: UUID, ids: list[UUID]) -> None:
    """Links rows as one move (ab-259: the two sides of a move inside a
    SACCO) without changing how each is handled."""
    await conn.execute(
        "UPDATE transactions SET transfer_group_id = $1 WHERE id = ANY($2::uuid[])", group_id, ids
    )


async def get_handled_transaction(conn: asyncpg.Connection, *, transaction_id: UUID, user_id: UUID) -> dict | None:
    """The row (locked) with its account's provider and the other leg's id
    when it's a paired transfer. None when it isn't the user's."""
    row = await conn.fetchrow(
        """
        SELECT t.id, t.account_id, a.provider, t.direction, t.amount, t.currency, t.description,
               t.handled_as, t.handled_reason, t.transfer_group_id, sl.role AS sub_ledger_role,
               (SELECT o.id FROM transactions o
                WHERE t.transfer_group_id IS NOT NULL AND o.transfer_group_id = t.transfer_group_id AND o.id <> t.id
                LIMIT 1) AS paired_transaction_id
        FROM transactions t
        JOIN accounts a ON a.id = t.account_id
        LEFT JOIN sub_ledgers sl ON sl.id = t.sub_ledger_id
        WHERE t.id = $1 AND a.user_id = $2
        FOR UPDATE OF t
        """,
        transaction_id,
        user_id,
    )
    return dict(row) if row else None


async def unlink_transfer(conn: asyncpg.Connection, *, transaction_ids: list[UUID]) -> None:
    await conn.execute(
        "UPDATE transactions SET transfer_group_id = NULL WHERE id = ANY($1::uuid[])", transaction_ids
    )


async def dismiss_handling(conn: asyncpg.Connection, *, transaction_id: UUID) -> None:
    await conn.execute(
        """
        UPDATE transactions
        SET handled_as = NULL, handled_reason = NULL, handling_dismissed = TRUE, transfer_group_id = NULL,
            handling_accepted_at = NULL
        WHERE id = $1
        """,
        transaction_id,
    )


async def get_category(conn: asyncpg.Connection, *, category_id: UUID, user_id: UUID) -> dict | None:
    row = await conn.fetchrow(
        "SELECT id, name, type FROM categories WHERE id = $1 AND user_id = $2", category_id, user_id
    )
    return dict(row) if row else None


async def get_import_to_handle(conn: asyncpg.Connection, *, import_id: UUID) -> dict | None:
    row = await conn.fetchrow(
        """
        SELECT si.id, si.account_id, a.provider, a.user_id, si.period_start, si.period_end,
               si.status, si.reviewed_at, si.handled_at
        FROM statement_imports si
        JOIN accounts a ON a.id = si.account_id
        WHERE si.id = $1
        """,
        import_id,
    )
    return dict(row) if row else None


# --- ab-285: transfers filed by hand ---------------------------------------


async def get_hand_filed_legs(conn: asyncpg.Connection, *, user_id: UUID) -> list[dict]:
    """Rows the user filed by hand as a Funds transfer from/to one of their
    own accounts (every allocation a transfer with that one account), not
    yet paired with the row on that account. ab-293: or with one deposits /
    shares sub-ledger - then source_sub_ledger_id is set and
    source_account_id is that sub-ledger's account."""
    rows = await conn.fetch(
        """
        SELECT t.id, t.account_id, a.nickname, t.direction, t.amount, t.currency, t.txn_date,
               src.source_account_id, src.source_sub_ledger_id
        FROM transactions t
        JOIN accounts a ON a.id = t.account_id
        JOIN LATERAL (
            SELECT COALESCE(MIN(al.source_account_id::text), MIN(ssl.account_id::text))::uuid AS source_account_id,
                   MIN(ssl.id::text)::uuid AS source_sub_ledger_id
            FROM allocations al
            LEFT JOIN sub_ledgers ssl
                ON ssl.id = al.source_sub_ledger_id AND ssl.role IN ('locked_saving', 'shares')
            WHERE al.transaction_id = t.id
            HAVING COUNT(*) > 0
               AND COUNT(*) = COUNT(al.transfer_reason_id)
               AND (
                    (COUNT(*) = COUNT(al.source_account_id) AND COUNT(DISTINCT al.source_account_id) = 1)
                 OR (COUNT(*) = COUNT(ssl.id) AND COUNT(DISTINCT ssl.id) = 1)
               )
        ) src ON true
        JOIN accounts sa ON sa.id = src.source_account_id AND sa.user_id = $1
        WHERE a.user_id = $1 AND t.handled_as IS NULL AND t.transfer_group_id IS NULL
        ORDER BY t.txn_date, t.id
        """,
        user_id,
    )
    return [dict(r) for r in rows]


async def get_hand_pair_candidates(
    conn: asyncpg.Connection, *, user_id: UUID, account_ids: list[UUID], amounts: list[float],
    from_date: date, to_date: date,
) -> list[dict]:
    """Rows on the named accounts that could be the other side: unpaired,
    never dismissed, and either a waiting transfer leg or still open - or
    (ab-293) a row handled as saving on a deposits / shares sub-ledger.
    Locked, so a concurrent pairing can't claim the same row."""
    rows = await conn.fetch(
        """
        SELECT t.id, t.account_id, a.nickname, t.direction, t.amount, t.currency, t.txn_date, t.handled_as,
               t.sub_ledger_id, sl.role AS sub_ledger_role
        FROM transactions t
        JOIN accounts a ON a.id = t.account_id
        LEFT JOIN sub_ledgers sl ON sl.id = t.sub_ledger_id
        WHERE a.user_id = $1
          AND t.account_id = ANY($2::uuid[])
          AND t.amount = ANY($3::numeric[])
          AND t.txn_date BETWEEN $4 AND $5
          AND t.transfer_group_id IS NULL
          AND NOT t.handling_dismissed
          AND (
                t.handled_as = 'transfer'
             OR (t.handled_as IS NULL AND NOT EXISTS (SELECT 1 FROM allocations al WHERE al.transaction_id = t.id))
             OR (t.handled_as = 'saving' AND sl.role IN ('locked_saving', 'shares'))
          )
        ORDER BY t.id
        FOR UPDATE OF t
        """,
        user_id,
        account_ids,
        amounts,
        from_date,
        to_date,
    )
    return [dict(r) for r in rows]


async def link_hand_pair(
    conn: asyncpg.Connection, *, group_id: UUID, leg_id: UUID, other_id: UUID, other_reason: str
) -> None:
    """The hand-filed row keeps its own reason; the other side becomes a
    handled transfer."""
    await conn.execute("UPDATE transactions SET transfer_group_id = $1 WHERE id = $2", group_id, leg_id)
    await conn.execute(
        """
        UPDATE transactions SET transfer_group_id = $1, handled_as = 'transfer', handled_reason = $3
        WHERE id = $2
        """,
        group_id,
        other_id,
        other_reason,
    )


async def get_hand_paired(conn: asyncpg.Connection, *, transaction_ids: list[UUID]) -> list[dict]:
    """For hand-filed rows (not handled at import) that are paired: the
    row and its other side."""
    rows = await conn.fetch(
        """
        SELECT t.id, o.id AS other_id
        FROM transactions t
        JOIN transactions o ON o.transfer_group_id = t.transfer_group_id AND o.id <> t.id
        WHERE t.id = ANY($1::uuid[]) AND t.handled_as IS NULL AND t.transfer_group_id IS NOT NULL
        """,
        transaction_ids,
    )
    return [dict(r) for r in rows]


async def get_rehandle_rows(conn: asyncpg.Connection, *, user_id: UUID, sub_ledger_id: UUID) -> list[dict]:
    """ab-288: a sub-ledger's rows nobody has touched - open with nothing
    allocated, or handled at import and never filed, edited, paired or
    marked "Not ..." by the user - locked for re-handling."""
    rows = await conn.fetch(
        """
        SELECT t.id, t.account_id, t.description, t.direction, t.amount, t.currency, t.handled_as,
               sl.role AS sub_ledger_role, a.provider
        FROM transactions t
        JOIN accounts a ON a.id = t.account_id
        JOIN sub_ledgers sl ON sl.id = t.sub_ledger_id
        WHERE t.sub_ledger_id = $1 AND a.user_id = $2
          AND t.transfer_group_id IS NULL
          AND NOT t.handling_dismissed
          AND t.handled_reason IS DISTINCT FROM 'Filed by you'
          AND NOT EXISTS (
              SELECT 1 FROM allocations al
              WHERE al.transaction_id = t.id AND (t.handled_as IS NULL OR al.apply_id IS NOT NULL)
          )
        ORDER BY t.txn_date, t.id
        FOR UPDATE OF t
        """,
        sub_ledger_id,
        user_id,
    )
    return [dict(r) for r in rows]
