"""ab-315: the mapping wizard - checking a user's mapping of an upload and
saving it as a shared layout.

A mapping is saved only when it reads at least two rows and its balance
check passes (the profile format already insists on a balance column or
the statement's totals to check against): it becomes an active profile every matching upload can use, so
it must prove itself on this file first - and, like every profile, again
on each new file.
"""

import io
import logging
from dataclasses import dataclass, field

import pdfplumber
from uuid import UUID

from app.core.config import get_settings
from app.core.queues import PARSE_JOBS_QUEUE
from app.core.redis import get_redis
from app.core.storage import get_storage_client
from app.parsers.base import StatementParseError
from app.parsers.profiles.guess import dedupe_roles
from app.parsers.profiles.layout import page_lines
from app.parsers.profiles.reader import read_statement
from app.parsers.profiles.tabular import load_grid
from app.parsers.profiles.text import normalise
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
        missing = missing_heading(content, profile)
    except Exception:
        logger.warning("A mapping's headings couldn't be looked for", exc_info=True)
        missing = None
    if missing:
        return LayoutCheck(
            passed=False,
            usable=False,
            message=f"We couldn't find \u201c{missing}\u201d on your statement. Check the spelling.",
            row_count=0,
        )
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


def missing_heading(content: bytes, profile: StatementProfile) -> str | None:
    """ab-325: the first column heading (as the user typed it) that isn't on
    the statement - page 1 of a PDF, or the first rows of a spreadsheet.
    Every word of the heading must be there; a heading over two lines
    still counts."""
    headers = [c.header for c in profile.table.columns if c.header]
    if not headers:
        return None
    if profile.file_type == "pdf":
        with pdfplumber.open(io.BytesIO(content)) as pdf:
            words = {normalise(w.text) for line in page_lines(pdf.pages[0]) for w in line.words}
    else:
        grid = load_grid(content, profile.file_type)
        words = {word for row in grid[:60] for value in row if isinstance(value, str) for word in normalise(value).split()}
    for header in headers:
        if not all(word in words for word in normalise(header).split()):
            return header
    return None


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


def apply_learned_roles(guess: dict, learned: dict[str, str]) -> tuple[dict, list[str]]:
    """ab-329: the guess with each column's role replaced by the one most
    users chose for its heading ({header: role}), single-use roles deduped
    again, and where each role came from ("learned" or "keyword"). The
    guess comes back unchanged if the learned roles don't make a valid
    profile."""
    columns = guess["table"]["columns"]
    keyword = ["keyword"] * len(columns)
    if not learned:
        return guess, keyword
    roles = dedupe_roles([learned.get(c.get("header") or "", c["role"]) for c in columns])
    candidate = {
        **guess,
        "table": {**guess["table"], "columns": [{**c, "role": r} for c, r in zip(columns, roles)]},
    }
    try:
        learned_guess = validate_profile(candidate).model_dump(mode="json")
    except Exception:
        logger.info("Learned column roles didn't make a valid layout; keeping the guess")
        return guess, keyword
    sources = [
        "learned" if c.get("header") in learned and r == learned[c["header"]] else "keyword"
        for c, r in zip(columns, roles)
    ]
    return learned_guess, sources
