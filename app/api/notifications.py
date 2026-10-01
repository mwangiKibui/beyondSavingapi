import logging
from datetime import datetime
from uuid import UUID

import asyncpg
from fastapi import APIRouter, Depends, HTTPException, Query, status
from pydantic import BaseModel

from app.core.db import get_pool
from app.core.errors import GENERIC_ERROR_MESSAGE
from app.core.security import get_current_user_id
from app.repositories.notifications import list_notifications, mark_notification_read

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/notifications", tags=["notifications"])

# Same allowed set as categories.py/transactions.py's own paginated
# listings (ab-24/ab-87) - kept manual rather than a Literal[...] type
# since Pydantic v2 doesn't reliably coerce a query string against int
# literals.
ALLOWED_PAGE_SIZES = (5, 10, 20, 30)


class NotificationItem(BaseModel):
    id: UUID
    type: str
    state: str | None
    title: str
    body: str | None
    budget_id: UUID | None
    plan_id: UUID | None
    read_at: datetime | None
    created_at: datetime


class NotificationListResponse(BaseModel):
    items: list[NotificationItem]
    total: int
    page: int
    page_size: int
    # The user's TOTAL unread count, regardless of the current page or the
    # unread_only filter - lets a nav badge render without a second call.
    unread_count: int


def _require_pool(pool: asyncpg.Pool | None) -> None:
    if pool is None:
        logger.error("Database pool unavailable")
        raise HTTPException(status_code=status.HTTP_503_SERVICE_UNAVAILABLE, detail=GENERIC_ERROR_MESSAGE)


@router.get("", response_model=NotificationListResponse)
async def list_notifications_endpoint(
    user_id: UUID = Depends(get_current_user_id),
    pool: asyncpg.Pool | None = Depends(get_pool),
    page: int = Query(default=1, ge=1),
    page_size: int = Query(default=10),
    unread_only: bool = Query(default=False),
) -> dict:
    if page_size not in ALLOWED_PAGE_SIZES:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail=f"page_size must be one of {ALLOWED_PAGE_SIZES}",
        )

    _require_pool(pool)

    try:
        items, total, unread_count = await list_notifications(
            pool,
            user_id=user_id,
            page=page,
            page_size=page_size,
            unread_only=unread_only,
        )
    except Exception:
        logger.error("Unexpected error listing notifications for user %s", user_id, exc_info=True)
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR, detail=GENERIC_ERROR_MESSAGE
        ) from None

    return {
        "items": items,
        "total": total,
        "page": page,
        "page_size": page_size,
        "unread_count": unread_count,
    }


@router.patch("/{notification_id}/read", response_model=NotificationItem)
async def mark_notification_read_endpoint(
    notification_id: UUID,
    user_id: UUID = Depends(get_current_user_id),
    pool: asyncpg.Pool | None = Depends(get_pool),
) -> dict:
    _require_pool(pool)

    try:
        notification = await mark_notification_read(pool, user_id=user_id, notification_id=notification_id)
        if notification is None:
            raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Notification not found")
    except HTTPException:
        raise
    except Exception:
        logger.error(
            "Unexpected error marking notification %s read for user %s", notification_id, user_id, exc_info=True
        )
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR, detail=GENERIC_ERROR_MESSAGE
        ) from None

    return notification
