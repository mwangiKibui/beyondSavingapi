"""ab-336: the AI category answer caches (beyondSavingdb migration 43).

merchant_ai_answers is shared: one answer per business name + direction.
user_category_ai_map is one user's mapping of an answer's category name onto
their own categories, for the category list it was made against."""

from uuid import UUID

import asyncpg

# An "unknown" answer is asked again after this long.
UNKNOWN_RETRY_DAYS = 90


async def get_answers(conn: asyncpg.Pool | asyncpg.Connection, *, names: list[tuple[str, str]]) -> dict:
    """{(name_key, direction): answer} for the fresh answers among names:
    every answer with a category, and "unknown" ones from the last 90 days."""
    if not names:
        return {}
    rows = await conn.fetch(
        """
        SELECT a.name_key, a.direction, a.category_name, a.is_new, a.confidence
        FROM merchant_ai_answers a
        JOIN unnest($1::text[], $2::text[]) AS w(name_key, direction)
          ON w.name_key = a.name_key AND w.direction = a.direction
        WHERE a.category_name IS NOT NULL OR a.answered_at > now() - make_interval(days => $3)
        """,
        [n for n, _ in names],
        [d for _, d in names],
        UNKNOWN_RETRY_DAYS,
    )
    return {(r["name_key"], r["direction"]): dict(r) for r in rows}


async def save_answers(conn: asyncpg.Pool | asyncpg.Connection, *, answers: list[dict], model: str) -> None:
    """answers: name_key, direction, category_name (None = unknown), is_new,
    confidence. A newer answer replaces an older one."""
    if not answers:
        return
    await conn.executemany(
        """
        INSERT INTO merchant_ai_answers (name_key, direction, category_name, is_new, confidence, model)
        VALUES ($1, $2, $3, $4, $5, $6)
        ON CONFLICT (name_key, direction) DO UPDATE
        SET category_name = EXCLUDED.category_name, is_new = EXCLUDED.is_new,
            confidence = EXCLUDED.confidence, model = EXCLUDED.model, answered_at = now()
        """,
        [
            (a["name_key"], a["direction"], a["category_name"], a["is_new"], a["confidence"], model)
            for a in answers
        ],
    )


async def get_user_map(
    conn: asyncpg.Pool | asyncpg.Connection, *, user_id: UUID, categories_hash: str
) -> dict[tuple[str, str], UUID | None]:
    """{(category_name, category_type): the user's category id, or None for
    "none of theirs fits"} made against this category list."""
    rows = await conn.fetch(
        """
        SELECT category_name, category_type, category_id FROM user_category_ai_map
        WHERE user_id = $1 AND categories_hash = $2
        """,
        user_id,
        categories_hash,
    )
    return {(r["category_name"], r["category_type"]): r["category_id"] for r in rows}


async def save_user_map(
    conn: asyncpg.Pool | asyncpg.Connection,
    *,
    user_id: UUID,
    categories_hash: str,
    mapping: dict[tuple[str, str], UUID | None],
) -> None:
    if not mapping:
        return
    await conn.executemany(
        """
        INSERT INTO user_category_ai_map (user_id, category_name, category_type, categories_hash, category_id)
        VALUES ($1, $2, $3, $4, $5)
        ON CONFLICT (user_id, category_name, category_type, categories_hash) DO UPDATE
        SET category_id = EXCLUDED.category_id
        """,
        [(user_id, name, kind, categories_hash, cid) for (name, kind), cid in mapping.items()],
    )


async def get_import_for_lookup(conn: asyncpg.Pool | asyncpg.Connection, *, import_id: UUID) -> dict | None:
    """What the lookup job needs about a live, read import."""
    row = await conn.fetchrow(
        """
        SELECT si.id, si.account_id, si.period_start, si.period_end, a.provider, a.user_id
        FROM statement_imports si
        JOIN accounts a ON a.id = si.account_id
        WHERE si.id = $1 AND si.status = 'parsed'
        """,
        import_id,
    )
    return dict(row) if row else None


# --- ab-339: one user's answers for masked descriptions (migration 44) --------


async def get_text_answers(
    conn: asyncpg.Pool | asyncpg.Connection, *, user_id: UUID, texts: list[tuple[str, str]]
) -> dict:
    """{(text_key, direction): answer} for this user's fresh answers among
    texts (unknown ones from the last 90 days)."""
    if not texts:
        return {}
    rows = await conn.fetch(
        """
        SELECT a.text_key, a.direction, a.answer_type, a.name, a.is_new
        FROM user_ai_text_answers a
        JOIN unnest($2::text[], $3::text[]) AS w(text_key, direction)
          ON w.text_key = a.text_key AND w.direction = a.direction
        WHERE a.user_id = $1
          AND (a.answer_type <> 'unknown' OR a.answered_at > now() - make_interval(days => $4))
        """,
        user_id,
        [t for t, _ in texts],
        [d for _, d in texts],
        UNKNOWN_RETRY_DAYS,
    )
    return {(r["text_key"], r["direction"]): dict(r) for r in rows}


async def save_text_answers(
    conn: asyncpg.Pool | asyncpg.Connection, *, user_id: UUID, answers: list[dict], model: str
) -> None:
    """answers: text_key, direction, answer_type, name, is_new."""
    if not answers:
        return
    await conn.executemany(
        """
        INSERT INTO user_ai_text_answers (user_id, text_key, direction, answer_type, name, is_new, model)
        VALUES ($1, $2, $3, $4, $5, $6, $7)
        ON CONFLICT (user_id, text_key, direction) DO UPDATE
        SET answer_type = EXCLUDED.answer_type, name = EXCLUDED.name, is_new = EXCLUDED.is_new,
            model = EXCLUDED.model, answered_at = now()
        """,
        [(user_id, a["text_key"], a["direction"], a["answer_type"], a["name"], a["is_new"], model) for a in answers],
    )
