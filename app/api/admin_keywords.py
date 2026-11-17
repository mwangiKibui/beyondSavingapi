"""Admin cleanup of the description keyword list (ab-290).

No approval queue (developer 2026-11-18, as with shared merchants): what
3+ users file a keyword under becomes the suggestion on its own. These
endpoints let an admin add, fix or retire keywords and see what users
chose instead - counts only, never who.
"""

import logging
import re
from datetime import datetime
from typing import Literal
from uuid import UUID

import asyncpg
from fastapi import APIRouter, Depends, HTTPException, Query, status
from pydantic import BaseModel, Field, field_validator

from app.core.db import get_pool
from app.core.errors import GENERIC_ERROR_MESSAGE
from app.core.security import require_admin
from app.repositories.description_keywords import (
    create_keyword,
    get_keyword,
    get_shared_keyword_choices,
    list_keywords,
    update_keyword,
)

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/admin", tags=["admin"])

ALLOWED_PAGE_SIZES = (5, 10, 20, 30)
Direction = Literal["in", "out"]
Status = Literal["approved", "retired"]


def _server_error(action: str, admin_id: UUID) -> HTTPException:
    logger.error("Unexpected error %s, admin %s", action, admin_id, exc_info=True)
    return HTTPException(status_code=status.HTTP_500_INTERNAL_SERVER_ERROR, detail=GENERIC_ERROR_MESSAGE)


def _clean_pattern(value: str) -> str:
    value = re.sub(r"\s+", " ", value.upper()).strip()
    if len(value.replace("*", "").strip()) < 2:
        raise ValueError("A keyword needs at least 2 letters or digits")
    if len(value) > 60:
        raise ValueError("A keyword must be 60 characters or fewer")
    return value


class VoteCount(BaseModel):
    category_name: str
    users: int


class KeywordOut(BaseModel):
    id: UUID
    pattern: str
    direction: str
    category_name: str
    status: str
    source: str
    # Users who filed this keyword under another category.
    users_choosing_otherwise: int = 0
    # What they chose (detail only).
    votes: list[VoteCount] | None = None
    created_at: datetime
    updated_at: datetime


class KeywordListResponse(BaseModel):
    items: list[KeywordOut]
    total: int
    page: int
    page_size: int


class KeywordCreate(BaseModel):
    pattern: str
    direction: Direction
    category_name: str = Field(min_length=1, max_length=100)

    _pattern = field_validator("pattern")(lambda cls, v: _clean_pattern(v))
    _name = field_validator("category_name")(lambda cls, v: v.strip())


class KeywordUpdate(BaseModel):
    pattern: str | None = None
    category_name: str | None = Field(default=None, min_length=1, max_length=100)
    # 'retired' stops suggesting it; users' choices are kept.
    status: Status | None = None

    _pattern = field_validator("pattern")(lambda cls, v: v if v is None else _clean_pattern(v))
    _name = field_validator("category_name")(lambda cls, v: v if v is None else v.strip())


_TAKEN = HTTPException(status_code=status.HTTP_409_CONFLICT, detail="That keyword already exists for this direction")


@router.get("/keywords", response_model=KeywordListResponse)
async def list_keywords_endpoint(
    page: int = Query(default=1, ge=1),
    page_size: int = Query(default=10),
    search: str | None = Query(default=None, min_length=1),
    direction: Direction | None = Query(default=None),
    status_filter: Status | None = Query(default=None, alias="status"),
    admin_id: UUID = Depends(require_admin),
    pool: asyncpg.Pool | None = Depends(get_pool),
) -> dict:
    if page_size not in ALLOWED_PAGE_SIZES:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail=f"page_size must be one of {ALLOWED_PAGE_SIZES}",
        )
    try:
        items, total = await list_keywords(
            pool, page=page, page_size=page_size, search=search, direction=direction, status=status_filter
        )
    except Exception:
        raise _server_error("listing keywords", admin_id) from None
    return {"items": items, "total": total, "page": page, "page_size": page_size}


async def _detail(conn, keyword_id: UUID) -> dict | None:
    keyword = await get_keyword(conn, keyword_id=keyword_id)
    if keyword is None:
        return None
    votes = await get_shared_keyword_choices(conn, keyword_ids=[keyword_id])
    return {**keyword, "votes": sorted(votes.get(keyword_id, []), key=lambda v: -v["users"])}


@router.get("/keywords/{keyword_id}", response_model=KeywordOut)
async def get_keyword_endpoint(
    keyword_id: UUID,
    admin_id: UUID = Depends(require_admin),
    pool: asyncpg.Pool | None = Depends(get_pool),
) -> dict:
    try:
        keyword = await _detail(pool, keyword_id)
    except Exception:
        raise _server_error("reading keyword", admin_id) from None
    if keyword is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Keyword not found")
    return keyword


@router.post("/keywords", response_model=KeywordOut, status_code=status.HTTP_201_CREATED)
async def create_keyword_endpoint(
    body: KeywordCreate,
    admin_id: UUID = Depends(require_admin),
    pool: asyncpg.Pool | None = Depends(get_pool),
) -> dict:
    """Adds a keyword by hand (source 'admin'); suggested at once."""
    try:
        keyword_id = await create_keyword(
            pool, admin_id=admin_id, pattern=body.pattern, direction=body.direction,
            category_name=body.category_name,
        )
        keyword = await _detail(pool, keyword_id)
    except asyncpg.UniqueViolationError:
        raise _TAKEN from None
    except Exception:
        raise _server_error("creating keyword", admin_id) from None
    return keyword


@router.patch("/keywords/{keyword_id}", response_model=KeywordOut)
async def update_keyword_endpoint(
    keyword_id: UUID,
    body: KeywordUpdate,
    admin_id: UUID = Depends(require_admin),
    pool: asyncpg.Pool | None = Depends(get_pool),
) -> dict:
    """Edits the pattern or category, or retires/restores a keyword."""
    try:
        found = await update_keyword(
            pool, keyword_id=keyword_id, pattern=body.pattern, category_name=body.category_name,
            status=body.status,
        )
        keyword = await _detail(pool, keyword_id) if found else None
    except asyncpg.UniqueViolationError:
        raise _TAKEN from None
    except Exception:
        raise _server_error("updating keyword", admin_id) from None
    if keyword is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Keyword not found")
    return keyword
