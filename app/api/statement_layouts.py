"""ab-315: the mapping wizard's API - an upload no stored layout could read
(status needs_mapping) is mapped by its uploader, checked, and saved as a
shared layout; or handed to support (awaiting_support).

Only the uploader ever sees the page words and rows returned here.
"""

import logging
from datetime import date
from typing import Literal, get_args
from uuid import UUID

import asyncpg
from fastapi import APIRouter, Depends, HTTPException, status
from pydantic import BaseModel
from starlette.concurrency import run_in_threadpool

from app.core.db import get_pool
from app.core.errors import GENERIC_ERROR_MESSAGE
from app.core.security import get_current_user_id
from app.parsers.profiles.guess import layout_start
from app.parsers.profiles.schema import InvalidStatementProfile, Role
from app.repositories.ai_layout_proposals import latest_proposed_profile
from app.repositories.column_heading_choices import learned_roles, record_choices
from app.repositories.notifications import notify_admins_layout_waiting
from app.repositories.statement_imports import get_import_for_layout, set_import_status
from app.services.statement_files import file_type_of
from app.services.statement_layouts import (
    apply_learned_roles,
    check_layout,
    load_content,
    parse_profile,
    save_layout,
)

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/statement-imports", tags=["statement-layouts"])

# Imports whose layout the uploader can still map.
_MAPPABLE = ("needs_mapping", "awaiting_support")

_ROLES = set(get_args(Role))


class PageWord(BaseModel):
    text: str
    x0: float
    x1: float


class PageLine(BaseModel):
    index: int
    top: float
    words: list[PageWord]


class PageSnapshot(BaseModel):
    width: float
    height: float
    lines: list[PageLine]


class LayoutStartResponse(BaseModel):
    file_type: Literal["pdf", "csv", "xlsx"]
    provider: str
    # Our first guess at the layout (a profile), or None if we found no table.
    guess: dict | None
    # ab-325: one sample cell from the statement per guessed column.
    examples: list[str] = []
    # PDF: page 1's lines with word positions.
    page: PageSnapshot | None = None
    page_count: int | None = None
    # CSV/XLSX: the first rows as text, and the heading row we think it is.
    rows: list[list[str]] | None = None
    header_row: int | None = None
    # ab-329: per guessed column, whether its role came from the heading's
    # words ("keyword") or from what most users chose for it ("learned");
    # ab-314: "ai" when the AI's proposal for this upload is the guess.
    suggestion_sources: list[Literal["keyword", "learned", "ai"]] = []


class LayoutRequest(BaseModel):
    profile: dict
    # ab-329: the role the wizard suggested per column of profile.table.columns
    # (None where it suggested nothing). Only recorded, never trusted.
    suggested_roles: list[str | None] | None = None


class PreviewRow(BaseModel):
    index: int
    txn_date: date
    description: str | None
    amount: float
    direction: Literal["in", "out"]
    balance_after: float | None


class LayoutCheckResponse(BaseModel):
    passed: bool
    # Passed with at least 2 rows: only a usable mapping can be saved.
    usable: bool
    message: str | None
    row_count: int
    failing_rows: list[int]
    rows: list[PreviewRow]
    opening_balance: float | None = None


class LayoutSavedResponse(BaseModel):
    status: Literal["pending"]
    profile_id: UUID


class SupportResponse(BaseModel):
    status: Literal["awaiting_support"]


def _require_pool(pool: asyncpg.Pool | None) -> None:
    if pool is None:
        logger.error("Database pool unavailable")
        raise HTTPException(status_code=status.HTTP_503_SERVICE_UNAVAILABLE, detail=GENERIC_ERROR_MESSAGE)


async def _mappable_import(pool, *, import_id: UUID, user_id: UUID) -> dict:
    statement_import = await get_import_for_layout(pool, import_id=import_id, user_id=user_id)
    if statement_import is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Statement import not found")
    if statement_import["status"] not in _MAPPABLE:
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail="This statement doesn't need mapping")
    return statement_import


def _file_type(statement_import: dict) -> str:
    return file_type_of(statement_import.get("file_name") or "") or "pdf"


def _profile_for(statement_import: dict, raw: dict):
    """Validates the mapping; its file type always follows the upload."""
    try:
        return parse_profile({**raw, "file_type": _file_type(statement_import)})
    except InvalidStatementProfile as e:
        raise HTTPException(status_code=status.HTTP_422_UNPROCESSABLE_ENTITY, detail=str(e)) from None


def _unexpected(action: str, import_id: UUID, user_id: UUID) -> HTTPException:
    logger.error("Unexpected error %s for import %s, user %s", action, import_id, user_id, exc_info=True)
    return HTTPException(status_code=status.HTTP_500_INTERNAL_SERVER_ERROR, detail=GENERIC_ERROR_MESSAGE)


@router.get("/{import_id}/layout", response_model=LayoutStartResponse)
async def get_layout_endpoint(
    import_id: UUID,
    user_id: UUID = Depends(get_current_user_id),
    pool: asyncpg.Pool | None = Depends(get_pool),
) -> dict:
    """Where the wizard starts: our guess, and page 1 (or the first rows)."""
    _require_pool(pool)
    try:
        statement_import = await _mappable_import(pool, import_id=import_id, user_id=user_id)
        file_type = _file_type(statement_import)
        content = await run_in_threadpool(load_content, statement_import["storage_key"])
        start = await run_in_threadpool(layout_start, content, file_type)
    except HTTPException:
        raise
    except Exception:
        raise _unexpected("starting the layout", import_id, user_id) from None
    guess, sources = start.get("guess"), []
    proposed = await _proposal(pool, import_id, guess)
    if proposed is not None:
        guess, sources = proposed, ["ai"] * len(proposed["table"]["columns"])
    elif guess is not None:
        guess, sources = await _with_learned_roles(pool, guess, file_type=file_type)
    return {
        "file_type": file_type,
        "provider": statement_import["provider"],
        **start,
        "guess": guess,
        "suggestion_sources": sources,
    }


async def _proposal(pool, import_id: UUID, guess: dict | None) -> dict | None:
    """ab-314: the AI's proposal for this upload (one that passed the balance
    check first), when we found no table ourselves or it has the same
    columns as today's guess (so the examples still line up). It was made
    for this file, so learned roles don't override it. A failed lookup just
    means no proposal."""
    try:
        proposed = await latest_proposed_profile(pool, import_id=import_id)
    except Exception:
        logger.error("Looking up the AI layout proposal for import %s failed", import_id, exc_info=True)
        return None
    if proposed is None or guess is None:
        return proposed
    headers = [c.get("header") for c in guess["table"]["columns"]]
    if [c.get("header") for c in proposed.get("table", {}).get("columns", [])] != headers:
        return None
    return proposed


async def _with_learned_roles(pool, guess: dict, *, file_type: str) -> tuple[dict, list[str]]:
    """The guess with the roles most users chose for its headings. A failed
    lookup just leaves the guess as it is."""
    columns = guess["table"]["columns"]
    try:
        learned = await learned_roles(pool, file_type=file_type, headings=[c.get("header") for c in columns])
        return apply_learned_roles(guess, learned)
    except Exception:
        logger.error("Looking up learned column roles failed", exc_info=True)
        return guess, ["keyword"] * len(columns)


async def _record_choices(pool, body: LayoutRequest, profile, *, user_id: UUID, file_type: str) -> None:
    """Remembers the role this user gave each heading. Never fails the save."""
    columns = profile.table.columns
    suggested = body.suggested_roles if body.suggested_roles and len(body.suggested_roles) == len(columns) else None
    choices = [
        (c.header, c.role, suggested[i] if suggested and suggested[i] in _ROLES else None)
        for i, c in enumerate(columns)
        if c.header
    ]
    try:
        await record_choices(pool, user_id=user_id, file_type=file_type, choices=choices)
    except Exception:
        logger.error("Recording column heading choices failed for user %s", user_id, exc_info=True)


@router.post("/{import_id}/layout/check", response_model=LayoutCheckResponse)
async def check_layout_endpoint(
    import_id: UUID,
    body: LayoutRequest,
    user_id: UUID = Depends(get_current_user_id),
    pool: asyncpg.Pool | None = Depends(get_pool),
) -> dict:
    """Reads the file with this mapping and checks its balances. Saves nothing."""
    _require_pool(pool)
    try:
        statement_import = await _mappable_import(pool, import_id=import_id, user_id=user_id)
        profile = _profile_for(statement_import, body.profile)
        content = await run_in_threadpool(load_content, statement_import["storage_key"])
        result = await run_in_threadpool(
            lambda: check_layout(content, profile, currency=statement_import.get("currency") or "KES")
        )
    except HTTPException:
        raise
    except Exception:
        raise _unexpected("checking a layout", import_id, user_id) from None
    return result.__dict__


@router.put("/{import_id}/layout", response_model=LayoutSavedResponse)
async def save_layout_endpoint(
    import_id: UUID,
    body: LayoutRequest,
    user_id: UUID = Depends(get_current_user_id),
    pool: asyncpg.Pool | None = Depends(get_pool),
) -> dict:
    """Saves a mapping that passes as a shared layout and reads the upload with it."""
    _require_pool(pool)
    try:
        statement_import = await _mappable_import(pool, import_id=import_id, user_id=user_id)
        if statement_import.get("institution_id") is None or not statement_import.get("fingerprint"):
            raise HTTPException(
                status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
                detail="Layouts can't be saved for this provider yet",
            )
        profile = _profile_for(statement_import, body.profile)
        content = await run_in_threadpool(load_content, statement_import["storage_key"])
        result = await run_in_threadpool(
            lambda: check_layout(content, profile, currency=statement_import.get("currency") or "KES")
        )
        if not result.usable:
            raise HTTPException(
                status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
                detail=result.message or "This mapping doesn't pass the balance check yet",
            )
        created = await save_layout(pool, statement_import=statement_import, profile=profile, user_id=user_id)
    except HTTPException:
        raise
    except Exception:
        raise _unexpected("saving a layout", import_id, user_id) from None
    await _record_choices(pool, body, profile, user_id=user_id, file_type=_file_type(statement_import))
    logger.info(
        "Import %s: layout saved as profile %s v%s", import_id, created["id"], created.get("version")
    )
    return {"status": "pending", "profile_id": created["id"]}


@router.post("/{import_id}/layout/support", response_model=SupportResponse)
async def send_to_support_endpoint(
    import_id: UUID,
    user_id: UUID = Depends(get_current_user_id),
    pool: asyncpg.Pool | None = Depends(get_pool),
) -> dict:
    """The uploader asks for help: the file is kept and admins are told."""
    _require_pool(pool)
    try:
        statement_import = await _mappable_import(pool, import_id=import_id, user_id=user_id)
        if statement_import["status"] == "awaiting_support":
            return {"status": "awaiting_support"}
        await set_import_status(pool, import_id=import_id, status="awaiting_support")
        try:
            await notify_admins_layout_waiting(pool, import_id=import_id, provider=statement_import["provider"])
        except Exception:
            # The upload still waits; the admin queue (ab-317) lists it anyway.
            logger.error("Telling admins about import %s failed", import_id, exc_info=True)
    except HTTPException:
        raise
    except Exception:
        raise _unexpected("sending to support", import_id, user_id) from None
    return {"status": "awaiting_support"}
