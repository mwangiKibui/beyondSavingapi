"""SQL for payee groups and category applies (ab-282). Every query is
scoped to the user through accounts.user_id - nothing ever reads or
touches another user's rows."""

import json
from datetime import datetime
from decimal import Decimal
from uuid import UUID

import asyncpg

from app.repositories.description_keywords import restore_keyword_choices

# Rows a user still has to categorise: statement rows with nothing
# allocated, not a transfer leg, not handled at import, and not part of an
# import whose review is still open (those wait in that review instead).
_UNCATEGORISED = """
    SELECT t.id, t.txn_date, t.amount, t.currency, t.direction, t.description, t.import_id,
           t.account_id, a.provider, a.nickname AS account_nickname, si.file_name,
           0::numeric AS allocated, NULL::uuid AS category_id, false AS is_transfer,
           NULL::text AS handled_as
    FROM transactions t
    JOIN accounts a ON a.id = t.account_id
    LEFT JOIN statement_imports si ON si.id = t.import_id
    WHERE a.user_id = $1
      AND t.source = 'statement'
      AND t.amount > 0
      AND t.transfer_group_id IS NULL
      AND t.handled_as IS NULL
      AND NOT EXISTS (SELECT 1 FROM allocations al WHERE al.transaction_id = t.id)
      AND (si.id IS NULL OR si.status <> 'parsed' OR si.reviewed_at IS NOT NULL)
"""


async def get_uncategorised_rows(pool: asyncpg.Pool | asyncpg.Connection, *, user_id: UUID) -> list[dict]:
    rows = await pool.fetch(_UNCATEGORISED + " ORDER BY t.txn_date, t.created_at, t.id", user_id)
    return [dict(r) for r in rows]


async def lock_rows_for_apply(
    conn: asyncpg.Connection, *, user_id: UUID, transaction_ids: list[UUID]
) -> list[dict]:
    """The user's rows among transaction_ids, locked until the apply
    commits, with what's already allocated to them."""
    rows = await conn.fetch(
        """
        SELECT t.id, t.txn_date, t.amount, t.currency, t.direction, t.description, t.import_id,
               t.account_id, a.provider, (t.transfer_group_id IS NOT NULL) AS is_transfer, t.handled_as,
               COALESCE((SELECT SUM(al.original_amount) FROM allocations al WHERE al.transaction_id = t.id), 0)
                   AS allocated,
               NULL::uuid AS category_id
        FROM transactions t
        JOIN accounts a ON a.id = t.account_id
        WHERE t.id = ANY($1::uuid[]) AND a.user_id = $2
        ORDER BY t.id
        FOR UPDATE OF t
        """,
        transaction_ids,
        user_id,
    )
    return [dict(r) for r in rows]


async def get_categories_for_user(conn: asyncpg.Connection, *, user_id: UUID, category_ids: list[UUID]) -> list[dict]:
    rows = await conn.fetch(
        "SELECT id, name, type FROM categories WHERE id = ANY($1::uuid[]) AND user_id = $2",
        category_ids,
        user_id,
    )
    return [dict(r) for r in rows]


async def insert_apply(
    conn: asyncpg.Connection,
    *,
    user_id: UUID,
    import_id: UUID | None,
    category_id: UUID,
    group_key: str,
    remember: bool,
    snapshot: dict,
) -> UUID:
    return await conn.fetchval(
        """
        INSERT INTO category_applies (user_id, import_id, category_id, group_key, remember, snapshot)
        VALUES ($1, $2, $3, $4, $5, $6::jsonb)
        RETURNING id
        """,
        user_id,
        import_id,
        category_id,
        group_key,
        remember,
        _json(snapshot),
    )


async def insert_applied_allocations(
    conn: asyncpg.Connection,
    *,
    apply_id: UUID,
    transaction_ids: list[UUID],
    category_id: UUID,
    amounts: list[float],
    currencies: list[str],
) -> None:
    """One allocation per row for its whole remaining amount, tagged with
    the apply so undo removes exactly these."""
    await conn.execute(
        """
        INSERT INTO allocations
            (transaction_id, category_id, original_amount, original_currency, amount, currency, apply_id)
        SELECT transaction_id, $2, amount, currency, amount, currency, $1
        FROM UNNEST($3::uuid[], $4::numeric[], $5::text[]) AS t(transaction_id, amount, currency)
        """,
        apply_id,
        category_id,
        transaction_ids,
        amounts,
        currencies,
    )


async def lock_apply(conn: asyncpg.Connection, *, apply_id: UUID, user_id: UUID) -> dict | None:
    row = await conn.fetchrow(
        """
        SELECT ca.id, ca.import_id, ca.category_id, ca.group_key, ca.remember, ca.snapshot, ca.undone_at,
               c.name AS category_name
        FROM category_applies ca
        LEFT JOIN categories c ON c.id = ca.category_id
        WHERE ca.id = $1 AND ca.user_id = $2
        FOR UPDATE OF ca
        """,
        apply_id,
        user_id,
    )
    if row is None:
        return None
    out = dict(row)
    if isinstance(out["snapshot"], str):
        out["snapshot"] = json.loads(out["snapshot"])
    return out


async def delete_apply_allocations(conn: asyncpg.Connection, *, apply_id: UUID) -> list[UUID]:
    rows = await conn.fetch("DELETE FROM allocations WHERE apply_id = $1 RETURNING transaction_id", apply_id)
    return sorted({r["transaction_id"] for r in rows})


async def delete_apply_line_allocations(
    conn: asyncpg.Connection, *, apply_id: UUID, transaction_ids: list[UUID]
) -> list[UUID]:
    """ab-292: takes some lines out of an apply, leaving the rest filed."""
    rows = await conn.fetch(
        "DELETE FROM allocations WHERE apply_id = $1 AND transaction_id = ANY($2::uuid[]) RETURNING transaction_id",
        apply_id, transaction_ids,
    )
    return sorted({r["transaction_id"] for r in rows})


async def count_apply_allocations(conn: asyncpg.Connection, *, apply_id: UUID) -> int:
    return await conn.fetchval("SELECT count(*) FROM allocations WHERE apply_id = $1", apply_id)


async def mark_apply_undone(conn: asyncpg.Connection, *, apply_id: UUID) -> None:
    await conn.execute("UPDATE category_applies SET undone_at = now() WHERE id = $1", apply_id)


# --- what remember changes, snapshotted for undo ----------------------------


async def get_rule_rows(conn: asyncpg.Connection, *, user_id: UUID, rule_keys: list[dict]) -> list[dict]:
    """The user's saved choices for each (key_kind, key_value, provider,
    account_hash, direction) - every category row, labels included."""
    if not rule_keys:
        return []
    rows = await conn.fetch(
        """
        SELECT r.key_kind, r.key_value, r.provider, r.account_hash, r.direction, r.category_id,
               r.label, r.user_entered_paybill, r.times_used, r.avg_amount, r.last_used_at
        FROM user_category_rules r
        JOIN unnest($2::text[], $3::text[], $4::text[], $5::text[], $6::text[])
             AS k(key_kind, key_value, provider, account_hash, direction)
          ON k.key_kind = r.key_kind AND k.key_value = r.key_value
         AND k.provider IS NOT DISTINCT FROM r.provider
         AND k.account_hash IS NOT DISTINCT FROM r.account_hash
         AND k.direction = r.direction
        WHERE r.user_id = $1
        """,
        user_id,
        *_columns(rule_keys, ("key_kind", "key_value", "provider", "account_hash", "direction")),
    )
    return [dict(r) for r in rows]


async def get_merchant_votes(conn: asyncpg.Connection, *, user_id: UUID, keys: list[dict]) -> list[dict]:
    if not keys:
        return []
    rows = await conn.fetch(
        """
        SELECT v.merchant_key, v.direction, v.category_name
        FROM merchant_directory_votes v
        JOIN unnest($2::text[], $3::text[]) AS k(merchant_key, direction)
          ON k.merchant_key = v.merchant_key AND k.direction = v.direction
        WHERE v.user_id = $1
        """,
        user_id,
        *_columns(keys, ("merchant_key", "direction")),
    )
    return [dict(r) for r in rows]


async def get_number_votes(conn: asyncpg.Connection, *, user_id: UUID, keys: list[dict]) -> list[dict]:
    if not keys:
        return []
    rows = await conn.fetch(
        """
        SELECT v.number_kind, v.number, v.direction, v.category_name
        FROM unknown_number_votes v
        JOIN unnest($2::text[], $3::text[], $4::text[]) AS k(number_kind, number, direction)
          ON k.number_kind = v.number_kind AND k.number = v.number AND k.direction = v.direction
        WHERE v.user_id = $1
        """,
        user_id,
        *_columns(keys, ("number_kind", "number", "direction")),
    )
    return [dict(r) for r in rows]


async def restore_snapshot(conn: asyncpg.Connection, *, user_id: UUID, snapshot: dict) -> None:
    """Puts the user's saved choices and shared votes for the snapshotted
    keys back exactly as they were before the apply."""
    # ab-290: description keyword choices (absent from older snapshots).
    keyword_ids = [UUID(str(k)) for k in snapshot.get("keyword_ids") or []]
    await restore_keyword_choices(
        conn, user_id=user_id, keyword_ids=keyword_ids, choices=snapshot.get("keyword_choices") or []
    )
    rule_keys = snapshot.get("rule_keys") or []
    if rule_keys:
        await conn.execute(
            """
            DELETE FROM user_category_rules r
            USING unnest($2::text[], $3::text[], $4::text[], $5::text[], $6::text[])
                  AS k(key_kind, key_value, provider, account_hash, direction)
            WHERE r.user_id = $1 AND k.key_kind = r.key_kind AND k.key_value = r.key_value
              AND k.provider IS NOT DISTINCT FROM r.provider
              AND k.account_hash IS NOT DISTINCT FROM r.account_hash
              AND k.direction = r.direction
            """,
            user_id,
            *_columns(rule_keys, ("key_kind", "key_value", "provider", "account_hash", "direction")),
        )
    rules = snapshot.get("rules") or []
    if rules:
        # A category deleted since is skipped rather than failing the undo.
        await conn.executemany(
            """
            INSERT INTO user_category_rules
                (user_id, key_kind, key_value, provider, account_hash, direction, category_id, label,
                 user_entered_paybill, times_used, avg_amount, last_used_at)
            SELECT $1, $2, $3, $4, $5, $6, $7, $8, $9, $10, $11, $12
            WHERE EXISTS (SELECT 1 FROM categories c WHERE c.id = $7 AND c.user_id = $1)
            """,
            [
                (user_id, r["key_kind"], r["key_value"], r["provider"], r["account_hash"], r["direction"],
                 UUID(r["category_id"]), r["label"], r["user_entered_paybill"], r["times_used"],
                 Decimal(r["avg_amount"]) if r["avg_amount"] is not None else None,
                 datetime.fromisoformat(r["last_used_at"]))
                for r in rules
            ],
        )

    for vote in snapshot.get("merchant_votes") or []:
        await conn.execute(
            "DELETE FROM merchant_directory_votes WHERE merchant_key = $1 AND direction = $2 AND user_id = $3",
            vote["merchant_key"], vote["direction"], user_id,
        )
        if vote.get("category_name"):
            await conn.execute(
                """
                INSERT INTO merchant_directory_votes (merchant_key, direction, user_id, category_name)
                VALUES ($1, $2, $3, $4)
                """,
                vote["merchant_key"], vote["direction"], user_id, vote["category_name"],
            )
    for vote in snapshot.get("number_votes") or []:
        await conn.execute(
            """
            DELETE FROM unknown_number_votes
            WHERE number_kind = $1 AND number = $2 AND direction = $3 AND user_id = $4
            """,
            vote["number_kind"], vote["number"], vote["direction"], user_id,
        )
        if vote.get("category_name"):
            await conn.execute(
                """
                INSERT INTO unknown_number_votes (number_kind, number, direction, user_id, category_name)
                VALUES ($1, $2, $3, $4, $5)
                """,
                vote["number_kind"], vote["number"], vote["direction"], user_id, vote["category_name"],
            )


def _columns(items: list[dict], names: tuple[str, ...]) -> list[list]:
    return [[item[n] for item in items] for n in names]


def _json(value) -> str:
    def default(o):
        if isinstance(o, (UUID, Decimal)):
            return str(o)
        if isinstance(o, datetime):
            return o.isoformat()
        raise TypeError(type(o))

    return json.dumps(value, default=default)
