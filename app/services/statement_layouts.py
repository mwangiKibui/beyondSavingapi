"""ab-315: the mapping wizard - checking a user's mapping of an upload and
saving it as a shared layout.

A mapping is saved only when it reads at least two rows and its balance
check passes (the profile format already insists on a balance column or
the statement's totals to check against): it becomes an active profile every matching upload can use, so
it must prove itself on this file first - and, like every profile, again
on each new file.
"""

import logging
from dataclasses import dataclass, field
from uuid import UUID

from app.core.config import get_settings
from app.core.queues import PARSE_JOBS_QUEUE
from app.core.redis import get_redis
from app.core.storage import get_storage_client
from app.parsers.base import StatementParseError
from app.parsers.profiles.reader import read_statement
from app.parsers.profiles.schema import StatementProfile, validate_profile
from app.repositories.statement_imports import list_waiting_imports, set_import_status
from app.repositories.statement_profiles import create_profile

logger = logging.getLogger(__name__)

PREVIEW_ROWS = 10
MIN_ROWS = 2


@dataclass
class LayoutCheck:
    passed: bool
    # Passed with enough rows to mean something. Only a usable mapping can
    # be saved.
    usable: bool
    message: str | None
    row_count: int
    failing_rows: list[int] = field(default_factory=list)
    # ~10 rows around the first failure (or the first 10), with their index.
    rows: list[dict] = field(default_factory=list)
    opening_balance: float | None = None


def check_layout(content: bytes, profile: StatementProfile, *, currency: str) -> LayoutCheck:
    """Runs the reader and its balance check; never saves anything. A layout
    that doesn't fit the file at all comes back as not passed, with why."""
    try:
        result = read_statement(content, profile, default_currency=currency)
    except StatementParseError as e:
        return LayoutCheck(passed=False, usable=False, message=str(e), row_count=0)
    except Exception:
        logger.warning("A mapping couldn't be read", exc_info=True)
        return LayoutCheck(passed=False, usable=False, message="This mapping couldn't read the file", row_count=0)

    txns = result.transactions
    failing = list(result.check.failing_rows)
    start = max(0, failing[0] - 3) if failing else 0
    window = [
        {
            "index": i,
            "txn_date": t["txn_date"],
            "description": t["description"],
            "amount": t["amount"],
            "direction": t["direction"],
            "balance_after": t["balance_after"],
        }
        for i, t in enumerate(txns[start : start + PREVIEW_ROWS], start=start)
    ]
    message = result.check.message
    usable = result.check.passed
    if usable and len(txns) < MIN_ROWS:
        usable, message = False, "We found fewer than 2 transactions with this mapping"
    return LayoutCheck(
        passed=result.check.passed,
        usable=usable,
        message=message,
        row_count=len(txns),
        failing_rows=failing,
        rows=window,
        opening_balance=result.opening_balance,
    )


def load_content(storage_key: str) -> bytes:
    settings = get_settings()
    return get_storage_client().get_object(Bucket=settings.storage_bucket, Key=storage_key)["Body"].read()


async def enqueue(import_id: UUID) -> None:
    await get_redis().lpush(PARSE_JOBS_QUEUE, str(import_id))


async def requeue_waiting(pool, *, institution_id: UUID) -> int:
    """Re-reads every upload of this institution waiting for support. They
    stay awaiting_support until a read succeeds (the worker then tells the
    uploader), so one that still doesn't fit just keeps waiting."""
    waiting = await list_waiting_imports(pool, institution_id=institution_id)
    for import_id in waiting:
        await enqueue(import_id)
    return len(waiting)


async def save_layout(
    pool, *, statement_import: dict, profile: StatementProfile, user_id: UUID
) -> dict:
    """Stores a usable mapping as an active, shared profile and re-reads the
    upload (and any others of its institution waiting for support)."""
    created = await create_profile(
        pool,
        institution_id=statement_import["institution_id"],
        fingerprint=statement_import["fingerprint"],
        profile=profile.model_dump(mode="json"),
        source="user",
        status="active",
        created_by=user_id,
    )
    await set_import_status(pool, import_id=statement_import["id"], status="pending")
    await enqueue(statement_import["id"])
    try:
        await requeue_waiting(pool, institution_id=statement_import["institution_id"])
    except Exception:
        logger.error("Re-reading uploads waiting for support failed", exc_info=True)
    return created


def parse_profile(raw: dict) -> StatementProfile:
    """validate_profile, kept here so the API has one import."""
    return validate_profile(raw)
