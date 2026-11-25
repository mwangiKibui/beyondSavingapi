"""ab-329: the role each user gave a column heading in the mapping wizard,
so a heading most people map the same way is suggested to the next
uploader.

Only heading text is stored (normalised), never a cell value. Each user
has one choice per file type and heading; choosing again replaces it, so
everyone counts once.
"""

from collections import defaultdict
from uuid import UUID

import asyncpg

from app.parsers.profiles.text import normalise

# A heading's learned role needs this many users, and a clear lead.
LEARN_MIN_USERS = 3
MAX_HEADING_LENGTH = 40


def normalise_heading(text: str | None) -> str | None:
    """The stored form (lower-case, single spaces, no trailing ':' or '.'),
    or None for an empty heading or one too long to store."""
    heading = normalise(text or "")
    if not heading or len(heading) > MAX_HEADING_LENGTH:
        return None
    return heading


async def record_choices(
    pool: asyncpg.Pool | asyncpg.Connection,
    *,
    user_id: UUID,
    file_type: str,
    choices: list[tuple[str | None, str, str | None]],
) -> None:
    """choices: (heading, role, suggested_role) per column. Headings that
    normalise to nothing are skipped; the last choice for a heading wins."""
    by_heading: dict[str, tuple[str, str | None]] = {}
    for heading, role, suggested_role in choices:
        key = normalise_heading(heading)
        if key is not None:
            by_heading[key] = (role, suggested_role)
    if not by_heading:
        return
    await pool.executemany(
        """
        INSERT INTO column_heading_choices (user_id, file_type, heading, role, suggested_role)
        VALUES ($1, $2, $3, $4, $5)
        ON CONFLICT (user_id, file_type, heading) DO UPDATE SET
            role = EXCLUDED.role,
            suggested_role = EXCLUDED.suggested_role,
            updated_at = now()
        """,
        [(user_id, file_type, h, role, suggested) for h, (role, suggested) in by_heading.items()],
    )


def pick_learned_role(votes: dict[str, int]) -> str | None:
    """The role most users chose, when at least LEARN_MIN_USERS chose it and
    it leads the runner-up outright (a tie learns nothing)."""
    ranked = sorted(votes.values(), reverse=True)
    if not ranked or ranked[0] < LEARN_MIN_USERS or (len(ranked) > 1 and ranked[1] == ranked[0]):
        return None
    return max(votes, key=votes.__getitem__)


async def learned_roles(
    pool: asyncpg.Pool | asyncpg.Connection, *, file_type: str, headings: list[str | None]
) -> dict[str, str]:
    """{heading as given: learned role} for the headings that have one.
    Counts distinct users per role; never says who."""
    normalised = {h: normalise_heading(h) for h in headings if h}
    keys = sorted({k for k in normalised.values() if k is not None})
    if not keys:
        return {}
    rows = await pool.fetch(
        """
        SELECT heading, role, count(DISTINCT user_id)::int AS users
        FROM column_heading_choices
        WHERE file_type = $1 AND heading = ANY($2::text[])
        GROUP BY heading, role
        """,
        file_type,
        keys,
    )
    votes: dict[str, dict[str, int]] = defaultdict(dict)
    for r in rows:
        votes[r["heading"]][r["role"]] = r["users"]
    winners = {heading: pick_learned_role(v) for heading, v in votes.items()}
    return {h: winners[k] for h, k in normalised.items() if k is not None and winners.get(k)}
