import logging
from datetime import datetime
from typing import Literal
from uuid import UUID

import asyncpg
from fastapi import APIRouter, Depends, HTTPException, Query, status
from pydantic import BaseModel, Field

from app.core.db import get_pool
from app.core.security import get_current_user_id
from app.repositories.categories import DuplicateCategory, create_category, list_categories

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/categories", tags=["categories"])

CategoryType = Literal["expense", "income"]


class CreateCategoryRequest(BaseModel):
    name: str = Field(min_length=1)
    type: CategoryType


class CategoryResponse(BaseModel):
    id: UUID
    name: str
    type: str
    is_default: bool
    created_at: datetime


ALLOWED_PAGE_SIZES = (5, 10, 20, 30)
SortBy = Literal["name"]
SortDir = Literal["asc", "desc"]


class CategoryListItem(BaseModel):
    id: UUID
    name: str
    type: str
    is_default: bool


class CategoryListResponse(BaseModel):
    items: list[CategoryListItem]
    total: int
    page: int
    page_size: int


@router.post("", response_model=CategoryResponse, status_code=status.HTTP_201_CREATED)
async def create_category_endpoint(
    payload: CreateCategoryRequest,
    user_id: UUID = Depends(get_current_user_id),
    pool: asyncpg.Pool | None = Depends(get_pool),
) -> dict:
    if pool is None:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE, detail="Database unavailable"
        )

    try:
        category = await create_category(
            pool, user_id=user_id, name=payload.name, category_type=payload.type
        )
    except DuplicateCategory as exc:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail="A category with that name already exists",
        ) from exc
    except Exception:
        logger.error("Unexpected error creating category for user %s", user_id, exc_info=True)
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail="Something went wrong. Please try again.",
        ) from None

    return category


@router.get("", response_model=CategoryListResponse)
async def list_categories_endpoint(
    category_type: CategoryType = Query(alias="type"),
    user_id: UUID = Depends(get_current_user_id),
    pool: asyncpg.Pool | None = Depends(get_pool),
    page: int = Query(default=1, ge=1),
    page_size: int = Query(default=10),
    search: str | None = Query(default=None, min_length=1),
    sort_by: SortBy = "name",
    sort_dir: SortDir = "asc",
) -> dict:
    # Same manual page_size validation as accounts.py - Literal[5, 10, 20,
    # 30] doesn't reliably coerce a query string against int literals.
    if page_size not in ALLOWED_PAGE_SIZES:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail=f"page_size must be one of {ALLOWED_PAGE_SIZES}",
        )

    if pool is None:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE, detail="Database unavailable"
        )

    try:
        items, total = await list_categories(
            pool,
            user_id=user_id,
            category_type=category_type,
            page=page,
            page_size=page_size,
            search=search,
            sort_by=sort_by,
            sort_dir=sort_dir,
        )
    except Exception:
        logger.error("Unexpected error listing categories for user %s", user_id, exc_info=True)
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail="Something went wrong. Please try again.",
        ) from None

    return {"items": items, "total": total, "page": page, "page_size": page_size}
