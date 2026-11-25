"""ab-314: AI layout proposals (ai_layout_proposals, migration 40). profile is
structure only - the model only ever saw a masked page."""

import json
from datetime import datetime
from uuid import UUID


async def insert_proposal(
    conn,
    *,
    user_id: UUID,
    import_id: UUID | None,
    model: str,
    outcome: str,
    profile: dict | None = None,
    prompt_tokens: int | None = None,
    completion_tokens: int | None = None,
) -> None:
    await conn.execute(
        """
        INSERT INTO ai_layout_proposals
            (user_id, statement_import_id, model, outcome, profile, prompt_tokens, completion_tokens)
        VALUES ($1, $2, $3, $4, $5::jsonb, $6, $7)
        """,
        user_id,
        import_id,
        model,
        outcome,
        json.dumps(profile) if profile is not None else None,
        prompt_tokens,
        completion_tokens,
    )


async def count_proposals(conn, *, user_id: UUID, start: datetime, end: datetime) -> int:
    """Model calls made for the user in [start, end) - every outcome counts,
    including failed calls, since each one was a request."""
    return await conn.fetchval(
        "SELECT COUNT(*) FROM ai_layout_proposals WHERE user_id = $1 AND created_at >= $2 AND created_at < $3",
        user_id,
        start,
        end,
    )


async def latest_proposed_profile(conn, *, import_id: UUID) -> dict | None:
    """The upload's proposal to suggest: the latest that passed the balance
    check, else the latest that read but failed it (still a better start
    than an empty Columns step)."""
    raw = await conn.fetchval(
        """
        SELECT profile FROM ai_layout_proposals
        WHERE statement_import_id = $1 AND outcome IN ('passed', 'failed_check')
        ORDER BY (outcome = 'passed') DESC, created_at DESC
        LIMIT 1
        """,
        import_id,
    )
    if raw is None:
        return None
    return json.loads(raw) if isinstance(raw, str) else raw
