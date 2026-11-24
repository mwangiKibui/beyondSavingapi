"""ab-309: plan limits. Every limit check in the API goes through
check_limit (read the user's plan value, count what they've used) or
enforce_limit (the same, raising the 429 the UI words).

A NULL value - or no row for the key on the user's plan - is unlimited,
and nothing is counted. Daily limits are calendar days in Nairobi time
(EAT, UTC+3 all year - Kenya has no daylight saving) and reset at the next
EAT midnight. accounts_max isn't daily (resets_at None). Users already over
a newly lowered limit keep what they have; they just can't add more
(used >= limit blocks)."""

from dataclasses import dataclass
from datetime import datetime, time, timedelta, timezone
from uuid import UUID

from fastapi import HTTPException, status

from app.repositories.plans import count_active_accounts, count_imports_reached_allocation, get_plan_limit

STATEMENT_IMPORTS_PER_DAY = "statement_imports_per_day"
ACCOUNTS_MAX = "accounts_max"
AI_LAYOUT_PROPOSALS_PER_DAY = "ai_layout_proposals_per_day"

# The only place the known keys are listed (admin edits are checked against it).
LIMIT_KEYS = (ACCOUNTS_MAX, AI_LAYOUT_PROPOSALS_PER_DAY, STATEMENT_IMPORTS_PER_DAY)
DAILY_KEYS = frozenset({STATEMENT_IMPORTS_PER_DAY, AI_LAYOUT_PROPOSALS_PER_DAY})

EAT = timezone(timedelta(hours=3), "EAT")


def nairobi_day(now: datetime) -> tuple[datetime, datetime]:
    """The EAT calendar day holding now, as [start, end) in EAT."""
    start = datetime.combine(now.astimezone(EAT).date(), time(0, 0), tzinfo=EAT)
    return start, start + timedelta(days=1)


@dataclass
class LimitStatus:
    key: str
    limit: int | None  # None = unlimited
    used: int | None  # None when unlimited (not counted)
    resets_at: datetime | None  # next EAT midnight for daily limits

    @property
    def reached(self) -> bool:
        return self.limit is not None and self.used is not None and self.used >= self.limit


class LimitReached(HTTPException):
    """The structured 429 at a limit; the UI words the message."""

    def __init__(self, limit: LimitStatus):
        super().__init__(
            status_code=status.HTTP_429_TOO_MANY_REQUESTS,
            detail={
                "code": "limit_reached",
                "limit_key": limit.key,
                "used": limit.used,
                "limit": limit.limit,
                "resets_at": limit.resets_at.isoformat() if limit.resets_at else None,
            },
        )


async def _count(conn, user_id: UUID, key: str, day: tuple[datetime, datetime]) -> int:
    if key == STATEMENT_IMPORTS_PER_DAY:
        return await count_imports_reached_allocation(conn, user_id=user_id, start=day[0], end=day[1])
    if key == ACCOUNTS_MAX:
        return await count_active_accounts(conn, user_id=user_id)
    # AI_LAYOUT_PROPOSALS_PER_DAY: nothing records proposals yet - ab-314
    # adds the usage table and the count; until then none have been used.
    return 0


async def check_limit(conn, user_id: UUID, key: str, *, now: datetime | None = None) -> LimitStatus:
    """conn: a pool or a connection (inside a transaction, the count sees
    that transaction's own inserts)."""
    if key not in LIMIT_KEYS:
        raise ValueError(f"Unknown limit key: {key}")
    day = nairobi_day(now or datetime.now(timezone.utc))
    resets_at = day[1] if key in DAILY_KEYS else None
    limit = await get_plan_limit(conn, user_id=user_id, key=key)
    if limit is None:
        return LimitStatus(key=key, limit=None, used=None, resets_at=resets_at)
    used = await _count(conn, user_id, key, day)
    return LimitStatus(key=key, limit=limit, used=used, resets_at=resets_at)


async def enforce_limit(conn, user_id: UUID, key: str, *, now: datetime | None = None) -> LimitStatus:
    """check_limit, raising LimitReached (429) when used >= limit."""
    limit = await check_limit(conn, user_id, key, now=now)
    if limit.reached:
        raise LimitReached(limit)
    return limit
