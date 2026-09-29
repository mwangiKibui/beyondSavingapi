import logging
from datetime import datetime
from typing import Literal
from uuid import UUID

import asyncpg
from fastapi import APIRouter, Depends, HTTPException, Query, status
from pydantic import BaseModel, Field

from app.core.db import get_pool
from app.core.errors import GENERIC_ERROR_MESSAGE
from app.core.security import get_current_user_id
from app.repositories.transfer_reasons import (
    DuplicateTransferReason,
    create_transfer_reason,
    list_transfer_reasons,
)

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/transfer-reasons", tags=["transfer-reasons"])

ALLOWED_PAGE_SIZES = (5, 10, 20, 30)
SortBy = Literal["name"]
SortDir = Literal["asc", "desc"]


class CreateTransferReasonRequest(BaseModel):
    name: str = Field(min_length=1)


class TransferReasonResponse(BaseModel):
    id: UUID
    name: str
    is_default: bool
    created_at: datetime


class TransferReasonListItem(BaseModel):
    id: UUID
    name: str
    is_default: bool


class TransferReasonListResponse(BaseModel):
    items: list[TransferReasonListItem]
    total: int
    page: int
    page_size: int


@router.post("", response_model=TransferReasonResponse, status_code=status.HTTP_201_CREATED)
async def create_transfer_reason_endpoint(
    payload: CreateTransferReasonRequest,
    user_id: UUID = Depends(get_current_user_id),
    pool: asyncpg.Pool | None = Depends(get_pool),
) -> dict:
    if pool is None:
        logger.error("Database pool unavailable")
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE, detail=GENERIC_ERROR_MESSAGE
        )

    try:
        reason = await create_transfer_reason(pool, user_id=user_id, name=payload.name)
    except DuplicateTransferReason as exc:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail="A transfer reason with that name already exists",
        ) from exc
    except Exception:
        logger.error("Unexpected error creating transfer reason for user %s", user_id, exc_info=True)
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail=GENERIC_ERROR_MESSAGE,
        ) from None

    return reason


@router.get("", response_model=TransferReasonListResponse)
async def list_transfer_reasons_endpoint(
    user_id: UUID = Depends(get_current_user_id),
    pool: asyncpg.Pool | None = Depends(get_pool),
    page: int = Query(default=1, ge=1),
    page_size: int = Query(default=10),
    search: str | None = Query(default=None, min_length=1),
    sort_by: SortBy = "name",
    sort_dir: SortDir = "asc",
) -> dict:
    if page_size not in ALLOWED_PAGE_SIZES:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail=f"page_size must be one of {ALLOWED_PAGE_SIZES}",
        )

    if pool is None:
        logger.error("Database pool unavailable")
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE, detail=GENERIC_ERROR_MESSAGE
        )

    try:
        items, total = await list_transfer_reasons(
            pool,
            user_id=user_id,
            page=page,
            page_size=page_size,
            search=search,
            sort_by=sort_by,
            sort_dir=sort_dir,
        )
    except Exception:
        logger.error("Unexpected error listing transfer reasons for user %s", user_id, exc_info=True)
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail=GENERIC_ERROR_MESSAGE,
        ) from None

    return {"items": items, "total": total, "page": page, "page_size": page_size}
