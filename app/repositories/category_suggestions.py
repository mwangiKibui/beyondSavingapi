"""Storage for statement-import category suggestions (ab-253): reads for
the preview, and the confirm step's writes. Functions take a pool or a
connection (same fetch/execute API); confirm passes one connection inside
a transaction so all its writes land or none do."""

from datetime import date
from uuid import UUID

import asyncpg


async def get_import_for_preview(pool: asyncpg.Pool, *, import_id: UUID, user_id: UUID) -> dict | None:
    # Scoped through accounts.user_id, like every other import read, so a
    # guessed import id from another user is simply not found.
    row = await pool.fetchrow(
        """
        SELECT si.id, si.account_id, a.nickname AS account_nickname, a.provider,
               si.file_name, si.status, si.period_start, si.period_end, si.row_count, si.created_at,
               si.reviewed_at
        FROM statement_imports si
        JOIN accounts a ON a.id = si.account_id
        WHERE si.id = $1 AND a.user_id = $2
        """,
        import_id,
        user_id,
    )
    return dict(row) if row else None


async def get_import_rows(
    pool: asyncpg.Pool | asyncpg.Connection,
    *,
    import_id: UUID,
    account_id: UUID,
    period_start: date | None,
    period_end: date | None,
) -> list[dict]:
    """Every transaction the statement covers, with its allocated total
    (summed on original_amount, as get_transaction does) and, when its
    allocations all use one category, that category_id - used for
    "already chosen in this statement".

    "Covers" means rows this import inserted, plus statement rows already
    on the same account within its period: a re-uploaded or overlapping
    statement has its rows skipped by dedupe (they stay linked to the
    earlier import), but the user still expects to see them. When the
    import was limited to chosen sub-ledgers, only those count. Manual
    entries never do."""
    rows = await pool.fetch(
        """
        -- No is_transfer column ships (docs/schema.sql is ahead): a row is
        -- a transfer leg when it has a transfer_group_id.
        SELECT t.id, t.txn_date, t.amount, t.currency, t.direction, t.description,
               (t.transfer_group_id IS NOT NULL) AS is_transfer,
               t.balance_after,
               COALESCE(alloc.total, 0) AS allocated,
               alloc.category_id,
               COALESCE(alloc.category_count, 0) AS category_count,
               COALESCE(alloc.has_uncategorised, false) AS has_uncategorised
        FROM transactions t
        LEFT JOIN LATERAL (
            SELECT SUM(al.original_amount) AS total,
                   CASE WHEN COUNT(*) = COUNT(al.category_id) AND COUNT(DISTINCT al.category_id) = 1
                        THEN MIN(al.category_id::text)::uuid END AS category_id,
                   COUNT(DISTINCT al.category_id) AS category_count,
                   bool_or(al.category_id IS NULL) AS has_uncategorised
            FROM allocations al
            WHERE al.transaction_id = t.id
        ) alloc ON true
        WHERE t.import_id = $1
           OR (
                t.account_id = $2
            AND t.source = 'statement'
            AND $3::date IS NOT NULL AND $4::date IS NOT NULL
            AND t.txn_date BETWEEN $3 AND $4
            AND (
                NOT EXISTS (SELECT 1 FROM statement_import_sub_ledgers s WHERE s.import_id = $1)
                OR t.sub_ledger_id IN (SELECT s.sub_ledger_id FROM statement_import_sub_ledgers s WHERE s.import_id = $1)
            )
           )
        ORDER BY t.txn_date, t.created_at, t.id
        """,
        import_id,
        account_id,
        period_start,
        period_end,
    )
    return [dict(row) for row in rows]


async def get_user_rules(pool: asyncpg.Pool, *, user_id: UUID, pairs: list[tuple[str, str]]) -> list[dict]:
    """The user's rules for the given (key_kind, key_value) pairs, in one
    query however many rows the statement has."""
    if not pairs:
        return []
    rows = await pool.fetch(
        """
        SELECT r.key_kind, r.key_value, r.provider, r.account_hash, r.direction, r.category_id,
               r.label, r.user_entered_paybill, r.times_used, r.avg_amount
        FROM user_category_rules r
        JOIN unnest($2::text[], $3::text[]) AS k(key_kind, key_value)
          ON k.key_kind = r.key_kind AND k.key_value = r.key_value
        WHERE r.user_id = $1
        """,
        user_id,
        [kind for kind, _ in pairs],
        [value for _, value in pairs],
    )
    return [dict(row) for row in rows]


async def get_approved_directory(pool: asyncpg.Pool) -> list[dict]:
    """Every approved shared-list merchant, one row per key. A few hundred
    merchants, so matching happens in Python rather than per row in SQL."""
    rows = await pool.fetch(
        """
        SELECT m.id AS merchant_id, m.display_name, m.category_name, m.direction,
               k.key_kind, k.key_value, k.requires_text
        FROM merchant_directory m
        JOIN merchant_directory_keys k ON k.merchant_id = m.id AND k.direction = m.direction
        WHERE m.status = 'approved'
        """
    )
    return [dict(row) for row in rows]


async def get_blocked_numbers(pool: asyncpg.Pool) -> list[dict]:
    rows = await pool.fetch("SELECT number_kind, number FROM blocked_numbers")
    return [dict(row) for row in rows]


async def get_user_categories(pool: asyncpg.Pool, *, user_id: UUID) -> list[dict]:
    rows = await pool.fetch("SELECT id, name, type FROM categories WHERE user_id = $1", user_id)
    return [dict(row) for row in rows]


# --- confirm (writes) -----------------------------------------------------

async def lock_transactions(conn: asyncpg.Connection, *, transaction_ids: list[UUID]) -> None:
    """Row locks until the confirm transaction ends, so two confirms of the
    same rows can't both see them unallocated and allocate twice."""
    await conn.execute(
        "SELECT 1 FROM transactions WHERE id = ANY($1::uuid[]) ORDER BY id FOR UPDATE", transaction_ids
    )


async def create_categories(
    conn: asyncpg.Connection, *, user_id: UUID, categories: list[tuple[str, str]]
) -> list[dict]:
    """Creates (name, type) categories. The caller has already matched
    names against the user's existing ones ignoring case; a clash anyway
    (a race) returns the existing row, unchanged, instead of failing the
    transaction. Uniqueness is idx_categories_user_type_lower_name
    (beyondSavingdb 000005), not the UNIQUE (user_id, name, type) that
    docs/schema.sql still shows."""
    if not categories:
        return []
    rows = await conn.fetch(
        """
        INSERT INTO categories (user_id, name, type)
        SELECT $1, name, type FROM unnest($2::text[], $3::text[]) AS c(name, type)
        ON CONFLICT (user_id, type, lower(name)) DO UPDATE SET name = categories.name
        RETURNING id, name, type
        """,
        user_id,
        [name for name, _ in categories],
        [category_type for _, category_type in categories],
    )
    return [dict(row) for row in rows]


async def upsert_rules(conn: asyncpg.Connection, *, user_id: UUID, rules: list[dict]) -> None:
    """One row per (key, account, direction, category): times_used grows by
    this confirm's row count and avg_amount stays a running average."""
    if not rules:
        return
    await conn.executemany(
        """
        INSERT INTO user_category_rules AS r
            (user_id, key_kind, key_value, provider, account_hash, direction, category_id,
             times_used, avg_amount, last_used_at)
        VALUES ($1, $2, $3, $4, $5, $6, $7, $8::int, round($9::numeric / $8::int, 2), now())
        ON CONFLICT ON CONSTRAINT user_category_rules_one_row_per_category DO UPDATE SET
            times_used = r.times_used + EXCLUDED.times_used,
            avg_amount = round(
                (COALESCE(r.avg_amount, EXCLUDED.avg_amount) * r.times_used + $9::numeric)
                / (r.times_used + EXCLUDED.times_used), 2),
            last_used_at = now(),
            updated_at = now()
        """,
        [
            (user_id, r["key_kind"], r["key_value"], r["provider"], r["account_hash"], r["direction"],
             r["category_id"], r["times"], r["total"])
            for r in rules
        ],
    )


async def set_rule_labels(conn: asyncpg.Connection, *, user_id: UUID, labels: list[dict]) -> None:
    """A label names the payee, so it goes on every category row for the
    key. user_entered_paybill is only ever kept on reference rows."""
    if not labels:
        return
    await conn.executemany(
        """
        UPDATE user_category_rules
        SET label = COALESCE($6, label),
            user_entered_paybill = CASE WHEN key_kind = 'reference'
                                        THEN COALESCE($7, user_entered_paybill)
                                        ELSE user_entered_paybill END,
            updated_at = now()
        WHERE user_id = $1 AND key_kind = $2 AND key_value = $3
          AND provider IS NOT DISTINCT FROM $4 AND direction = $5
        """,
        [
            (user_id, x["key_kind"], x["key_value"], x["provider"], x["direction"], x["label"],
             x["user_entered_paybill"])
            for x in labels
        ],
    )


async def upsert_merchant_votes(conn: asyncpg.Connection, *, user_id: UUID, votes: list[dict]) -> None:
    """One vote per user per business name and direction; a later confirm
    replaces it. Read back by get_shared_votes - no admin step."""
    if not votes:
        return
    await conn.executemany(
        """
        INSERT INTO merchant_directory_votes (merchant_key, direction, user_id, category_name)
        VALUES ($1, $2, $3, $4)
        ON CONFLICT (merchant_key, direction, user_id) DO UPDATE
            SET category_name = EXCLUDED.category_name, updated_at = now()
        """,
        [(v["merchant_key"], v["direction"], user_id, v["category_name"]) for v in votes],
    )


async def upsert_number_votes(conn: asyncpg.Connection, *, user_id: UUID, votes: list[dict]) -> None:
    if not votes:
        return
    await conn.executemany(
        """
        INSERT INTO unknown_number_votes (number_kind, number, direction, user_id, category_name)
        VALUES ($1, $2, $3, $4, $5)
        ON CONFLICT (number_kind, number, direction, user_id) DO UPDATE
            SET category_name = EXCLUDED.category_name, updated_at = now()
        """,
        [(v["number_kind"], v["number"], v["direction"], user_id, v["category_name"]) for v in votes],
    )


async def mark_import_reviewed(conn: asyncpg.Connection, *, import_id: UUID):
    return await conn.fetchval(
        "UPDATE statement_imports SET reviewed_at = now() WHERE id = $1 RETURNING reviewed_at", import_id
    )


async def get_shared_votes(
    pool: asyncpg.Pool | asyncpg.Connection, *, name_keys: list[tuple], number_keys: list[tuple]
) -> dict[tuple, list[dict]]:
    """Vote totals per category (distinct users; category names grouped
    ignoring case) for the preview's rows, in two queries however many rows.

    name_keys:   [('name', merchant_key, direction)]
    number_keys: [(number_kind, number, direction)]
    Returns {key tuple: [{category_name, users}]} - counts only, never who."""
    out: dict[tuple, list[dict]] = {}
    if name_keys:
        rows = await pool.fetch(
            """
            SELECT v.merchant_key, v.direction, min(v.category_name) AS category_name,
                   count(DISTINCT v.user_id) AS users
            FROM merchant_directory_votes v
            JOIN unnest($1::text[], $2::text[]) AS k(merchant_key, direction)
              ON k.merchant_key = v.merchant_key AND k.direction = v.direction
            GROUP BY v.merchant_key, v.direction, lower(v.category_name)
            """,
            [k[1] for k in name_keys],
            [k[2] for k in name_keys],
        )
        for r in rows:
            out.setdefault(("name", r["merchant_key"], r["direction"]), []).append(
                {"category_name": r["category_name"], "users": r["users"]}
            )
    if number_keys:
        rows = await pool.fetch(
            """
            SELECT v.number_kind, v.number, v.direction, min(v.category_name) AS category_name,
                   count(DISTINCT v.user_id) AS users
            FROM unknown_number_votes v
            JOIN unnest($1::text[], $2::text[], $3::text[]) AS k(number_kind, number, direction)
              ON k.number_kind = v.number_kind AND k.number = v.number AND k.direction = v.direction
            GROUP BY v.number_kind, v.number, v.direction, lower(v.category_name)
            """,
            [k[0] for k in number_keys],
            [k[1] for k in number_keys],
            [k[2] for k in number_keys],
        )
        for r in rows:
            out.setdefault((r["number_kind"], r["number"], r["direction"]), []).append(
                {"category_name": r["category_name"], "users": r["users"]}
            )
    return out


# --- "use this from now on" (update_rule) ---------------------------------


async def get_rows_for_rule_update(pool: asyncpg.Pool, *, transaction_ids: list[UUID], user_id: UUID) -> list[dict]:
    """What's needed to key each row: its text, direction, amount and the
    account's provider. Scoped to the user."""
    rows = await pool.fetch(
        """
        SELECT t.id, t.description, t.direction, t.amount, a.provider
        FROM transactions t
        JOIN accounts a ON a.id = t.account_id
        WHERE t.id = ANY($1::uuid[]) AND a.user_id = $2
        """,
        transaction_ids,
        user_id,
    )
    return [dict(r) for r in rows]


async def set_preferred_rules(pool: asyncpg.Pool, *, user_id: UUID, preferences: list[dict]) -> None:
    """Makes each chosen category the top one for its key: times_used is set
    one above the highest OTHER category for the same key/account/direction
    (and never below its own count + 1), so pick_rule suggests it next."""
    if not preferences:
        return
    await pool.executemany(
        """
        WITH others AS (
            SELECT COALESCE(max(o.times_used), 0) AS top
            FROM user_category_rules o
            WHERE o.user_id = $1 AND o.key_kind = $2 AND o.key_value = $3
              AND o.provider IS NOT DISTINCT FROM $4 AND o.account_hash IS NOT DISTINCT FROM $5
              AND o.direction = $6 AND o.category_id <> $7
        )
        INSERT INTO user_category_rules AS r
            (user_id, key_kind, key_value, provider, account_hash, direction, category_id,
             times_used, avg_amount, last_used_at)
        SELECT $1, $2, $3, $4, $5, $6, $7, others.top + 1, round($8::numeric, 2), now() FROM others
        ON CONFLICT ON CONSTRAINT user_category_rules_one_row_per_category DO UPDATE SET
            times_used = GREATEST(r.times_used + 1,
                                  (SELECT top FROM others) + 1),
            avg_amount = round((COALESCE(r.avg_amount, $8::numeric) * r.times_used + $8::numeric)
                               / (r.times_used + 1), 2),
            last_used_at = now(),
            updated_at = now()
        """,
        [
            (user_id, p["key_kind"], p["key_value"], p["provider"], p["account_hash"], p["direction"],
             p["category_id"], p["amount"])
            for p in preferences
        ],
    )
