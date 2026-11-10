"""Admin cleanup of the shared merchant list (ab-253 PR 3; UI is ab-255).

No approval queue (decision 2026-11-10): what most users choose becomes a
suggestion on its own. These endpoints let an admin fix or extend the
list, see what users have agreed on, and work the "Unknown numbers" lead.
Vote data is counts only - never who voted.
"""

import logging
import re
from datetime import datetime
from typing import Literal
from uuid import UUID

import asyncpg
from fastapi import APIRouter, Depends, HTTPException, Query, Response, status
from pydantic import BaseModel, Field, field_validator, model_validator

from app.core.db import get_pool
from app.core.errors import GENERIC_ERROR_MESSAGE
from app.core.security import require_admin
from app.repositories.merchant_admin import (
    create_merchant,
    delete_merchant,
    dismiss_unknown_number,
    find_conflicts,
    get_blocked,
    get_merchant,
    list_from_users,
    list_merchants,
    list_unknown_numbers,
    update_merchant,
)
from app.services.category_suggestions import SHARED_MIN_VOTERS

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/admin", tags=["admin"])

ALLOWED_PAGE_SIZES = (5, 10, 20, 30)
UNKNOWN_NUMBER_MIN_USERS = 10
Direction = Literal["in", "out"]
Source = Literal["starter", "users", "ai", "admin"]
SortDir = Literal["asc", "desc"]


def _check_page_size(page_size: int) -> None:
    if page_size not in ALLOWED_PAGE_SIZES:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail=f"page_size must be one of {ALLOWED_PAGE_SIZES}",
        )


def _server_error(action: str, admin_id: UUID) -> HTTPException:
    logger.error("Unexpected error %s, admin %s", action, admin_id, exc_info=True)
    return HTTPException(status_code=status.HTTP_500_INTERNAL_SERVER_ERROR, detail=GENERIC_ERROR_MESSAGE)


# --- models ---------------------------------------------------------------


class MerchantKeyIn(BaseModel):
    key_kind: Literal["name_pattern", "paybill", "till"]
    key_value: str
    # Numbers only: the number counts only when the row's text also matches
    # one of this merchant's name patterns (shared collection numbers).
    requires_text: bool = False

    @model_validator(mode="after")
    def _check(self):
        if self.key_kind == "name_pattern":
            value = re.sub(r"\s+", " ", self.key_value.upper()).strip()
            if not value.replace("*", "").strip():
                raise ValueError("A name pattern needs some text")
            if len(value) > 80:
                raise ValueError("A name pattern must be 80 characters or fewer")
            if self.requires_text:
                raise ValueError("requires_text only applies to Paybill/Till numbers")
            self.key_value = value
        else:
            value = self.key_value.strip()
            if not re.fullmatch(r"[0-9]{4,8}", value):
                raise ValueError("A Paybill/Till number is 4-8 digits")
            self.key_value = value
        return self


class MerchantKeyOut(BaseModel):
    key_kind: str
    key_value: str
    requires_text: bool


class VoteCount(BaseModel):
    category_name: str
    users: int


class MerchantOut(BaseModel):
    id: UUID
    display_name: str
    category_name: str
    direction: str
    source: str
    status: str
    notes: str | None = None
    keys: list[MerchantKeyOut]
    # Distinct users per category from votes on this merchant's numbers and
    # plain names - counts only.
    votes: list[VoteCount]
    created_at: datetime
    updated_at: datetime


class MerchantListResponse(BaseModel):
    items: list[MerchantOut]
    total: int
    page: int
    page_size: int


def _clean_text(value: str, *, field: str, limit: int) -> str:
    value = re.sub(r"\s+", " ", value).strip()
    if not value:
        raise ValueError(f"{field} can't be blank")
    if len(value) > limit:
        raise ValueError(f"{field} must be {limit} characters or fewer")
    return value


class MerchantCreate(BaseModel):
    display_name: str
    category_name: str
    direction: Direction
    keys: list[MerchantKeyIn] = Field(min_length=1, max_length=50)

    @field_validator("display_name")
    @classmethod
    def _name(cls, v):
        return _clean_text(v, field="Name", limit=80)

    @field_validator("category_name")
    @classmethod
    def _category(cls, v):
        return _clean_text(v, field="Category name", limit=60)


class MerchantUpdate(BaseModel):
    display_name: str | None = None
    category_name: str | None = None
    # Replaces the merchant's keys as a set when given.
    keys: list[MerchantKeyIn] | None = Field(default=None, min_length=1, max_length=50)

    @field_validator("display_name")
    @classmethod
    def _name(cls, v):
        return v if v is None else _clean_text(v, field="Name", limit=80)

    @field_validator("category_name")
    @classmethod
    def _category(cls, v):
        return v if v is None else _clean_text(v, field="Category name", limit=60)


class VoteGroup(BaseModel):
    key_kind: str  # 'name' | 'paybill' | 'till'
    key_value: str
    direction: str
    users: int
    votes: list[VoteCount]
    on_list: bool | None = None


class VoteGroupListResponse(BaseModel):
    items: list[VoteGroup]
    total: int
    page: int
    page_size: int


class DismissNumberRequest(BaseModel):
    number_kind: Literal["paybill", "till"]
    number: str = Field(pattern=r"^[0-9]{4,8}$")
    direction: Direction


# --- helpers --------------------------------------------------------------


def _key_dicts(keys: list[MerchantKeyIn]) -> list[dict]:
    seen, out = set(), []
    for k in keys:
        if (k.key_kind, k.key_value) in seen:
            continue
        seen.add((k.key_kind, k.key_value))
        out.append(k.model_dump())
    return out


async def _check_keys(conn, *, display_name: str, direction: str, keys: list[dict] | None, exclude: UUID | None):
    """422 for blocked numbers, 409 for clashes with another merchant."""
    if keys is not None:
        numbers = [(k["key_kind"], k["key_value"]) for k in keys if k["key_kind"] != "name_pattern"]
        blocked = await get_blocked(conn, numbers=numbers)
        if blocked:
            b = blocked[0]
            raise HTTPException(
                status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
                detail=(f"{b['number_kind'].capitalize()} {b['number']} is shared or wrongly attributed "
                        f"({b['belongs_to']}) and can't identify one merchant"),
            )
    problems = await find_conflicts(
        conn,
        display_name=display_name,
        direction=direction,
        keys=[(k["key_kind"], k["key_value"]) for k in keys or []],
        exclude_merchant_id=exclude,
    )
    if problems:
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail="; ".join(problems))


# --- merchants ------------------------------------------------------------


@router.get("/merchants", response_model=MerchantListResponse)
async def list_merchants_endpoint(
    page: int = Query(default=1, ge=1),
    page_size: int = Query(default=10),
    search: str | None = Query(default=None, min_length=1),
    source: Source | None = Query(default=None),
    direction: Direction | None = Query(default=None),
    sort_dir: SortDir = "asc",
    admin_id: UUID = Depends(require_admin),
    pool: asyncpg.Pool | None = Depends(get_pool),
) -> dict:
    _check_page_size(page_size)
    try:
        items, total = await list_merchants(
            pool, page=page, page_size=page_size, search=search, source=source, direction=direction,
            sort_dir=sort_dir,
        )
    except Exception:
        raise _server_error("listing merchants", admin_id) from None
    return {"items": items, "total": total, "page": page, "page_size": page_size}


@router.get("/merchants/from-users", response_model=VoteGroupListResponse)
async def list_from_users_endpoint(
    page: int = Query(default=1, ge=1),
    page_size: int = Query(default=10),
    search: str | None = Query(default=None, min_length=1),
    admin_id: UUID = Depends(require_admin),
    pool: asyncpg.Pool | None = Depends(get_pool),
) -> dict:
    """Business names and numbers that enough users voted on to be suggested
    to everyone, with the breakdown and whether the shared list already has
    them - for pinning one as a proper merchant."""
    _check_page_size(page_size)
    try:
        items, total = await list_from_users(
            pool, min_users=SHARED_MIN_VOTERS, page=page, page_size=page_size, search=search
        )
    except Exception:
        raise _server_error("listing merchants from users", admin_id) from None
    return {"items": items, "total": total, "page": page, "page_size": page_size}


@router.get("/merchants/{merchant_id}", response_model=MerchantOut)
async def get_merchant_endpoint(
    merchant_id: UUID,
    admin_id: UUID = Depends(require_admin),
    pool: asyncpg.Pool | None = Depends(get_pool),
) -> dict:
    try:
        merchant = await get_merchant(pool, merchant_id=merchant_id)
    except Exception:
        raise _server_error("reading merchant", admin_id) from None
    if merchant is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Merchant not found")
    return merchant


@router.post("/merchants", response_model=MerchantOut, status_code=status.HTTP_201_CREATED)
async def create_merchant_endpoint(
    body: MerchantCreate,
    admin_id: UUID = Depends(require_admin),
    pool: asyncpg.Pool | None = Depends(get_pool),
) -> dict:
    """Adds a merchant by hand (source 'admin'); suggested at once."""
    keys = _key_dicts(body.keys)
    try:
        async with pool.acquire() as conn:
            async with conn.transaction():
                await _check_keys(conn, display_name=body.display_name, direction=body.direction, keys=keys,
                                  exclude=None)
                merchant_id = await create_merchant(
                    conn, admin_id=admin_id, display_name=body.display_name, category_name=body.category_name,
                    direction=body.direction, keys=keys,
                )
                merchant = await get_merchant(conn, merchant_id=merchant_id)
    except HTTPException:
        raise
    except asyncpg.UniqueViolationError:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT, detail="That name or key was just taken by another merchant"
        ) from None
    except Exception:
        raise _server_error("creating merchant", admin_id) from None
    return merchant


@router.patch("/merchants/{merchant_id}", response_model=MerchantOut)
async def update_merchant_endpoint(
    merchant_id: UUID,
    body: MerchantUpdate,
    admin_id: UUID = Depends(require_admin),
    pool: asyncpg.Pool | None = Depends(get_pool),
) -> dict:
    """Edits name, category and/or keys (keys replaced as a set)."""
    keys = _key_dicts(body.keys) if body.keys is not None else None
    try:
        async with pool.acquire() as conn:
            async with conn.transaction():
                current = await get_merchant(conn, merchant_id=merchant_id)
                if current is None:
                    raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Merchant not found")
                display_name = body.display_name or current["display_name"]
                await _check_keys(conn, display_name=display_name, direction=current["direction"], keys=keys,
                                  exclude=merchant_id)
                await update_merchant(
                    conn, merchant_id=merchant_id, display_name=display_name,
                    category_name=body.category_name or current["category_name"],
                    direction=current["direction"], keys=keys,
                )
                merchant = await get_merchant(conn, merchant_id=merchant_id)
    except HTTPException:
        raise
    except asyncpg.UniqueViolationError:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT, detail="That name or key was just taken by another merchant"
        ) from None
    except Exception:
        raise _server_error("updating merchant", admin_id) from None
    return merchant


@router.delete("/merchants/{merchant_id}", status_code=status.HTTP_204_NO_CONTENT, response_class=Response)
async def delete_merchant_endpoint(
    merchant_id: UUID,
    admin_id: UUID = Depends(require_admin),
    pool: asyncpg.Pool | None = Depends(get_pool),
) -> Response:
    """Removes a merchant and its keys. Users' own rules and votes stay."""
    try:
        deleted = await delete_merchant(pool, merchant_id=merchant_id)
    except Exception:
        raise _server_error("deleting merchant", admin_id) from None
    if not deleted:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Merchant not found")
    return Response(status_code=status.HTTP_204_NO_CONTENT)


# --- unknown numbers ------------------------------------------------------


@router.get("/unknown-numbers", response_model=VoteGroupListResponse)
async def list_unknown_numbers_endpoint(
    page: int = Query(default=1, ge=1),
    page_size: int = Query(default=10),
    admin_id: UUID = Depends(require_admin),
    pool: asyncpg.Pool | None = Depends(get_pool),
) -> dict:
    """Paybill/Till numbers 10+ users categorised that the shared list has
    no key for. Turn one into a merchant with POST /admin/merchants."""
    _check_page_size(page_size)
    try:
        items, total = await list_unknown_numbers(
            pool, min_users=UNKNOWN_NUMBER_MIN_USERS, page=page, page_size=page_size
        )
    except Exception:
        raise _server_error("listing unknown numbers", admin_id) from None
    return {"items": items, "total": total, "page": page, "page_size": page_size}


@router.post("/unknown-numbers/dismiss", status_code=status.HTTP_204_NO_CONTENT, response_class=Response)
async def dismiss_unknown_number_endpoint(
    body: DismissNumberRequest,
    admin_id: UUID = Depends(require_admin),
    pool: asyncpg.Pool | None = Depends(get_pool),
) -> Response:
    try:
        await dismiss_unknown_number(
            pool, admin_id=admin_id, number_kind=body.number_kind, number=body.number, direction=body.direction
        )
    except Exception:
        raise _server_error("dismissing unknown number", admin_id) from None
    return Response(status_code=status.HTTP_204_NO_CONTENT)
