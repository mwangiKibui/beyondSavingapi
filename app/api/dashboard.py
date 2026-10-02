import logging
from datetime import date
from decimal import Decimal
from uuid import UUID

import asyncpg
from fastapi import APIRouter, Depends, HTTPException, Query, status
from pydantic import BaseModel

from app.core.db import get_pool
from app.core.errors import GENERIC_ERROR_MESSAGE
from app.core.security import get_current_user_id
from app.repositories.dashboard import get_dashboard, resolve_dashboard_currency

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/dashboard", tags=["dashboard"])


class AccountPerformance(BaseModel):
    account_id: UUID
    account_nickname: str
    money_in: Decimal
    money_out: Decimal


class DashboardResponse(BaseModel):
    currency: str
    total_budget: Decimal
    total_in: Decimal
    total_out: Decimal
    net: Decimal
    accounts: list[AccountPerformance]


def _require_pool(pool: asyncpg.Pool | None) -> None:
    if pool is None:
        logger.error("Database pool unavailable")
        raise HTTPException(status_code=status.HTTP_503_SERVICE_UNAVAILABLE, detail=GENERIC_ERROR_MESSAGE)


@router.get("", response_model=DashboardResponse)
async def get_dashboard_endpoint(
    from_date: date = Query(alias="from"),
    to_date: date = Query(alias="to"),
    user_id: UUID = Depends(get_current_user_id),
    pool: asyncpg.Pool | None = Depends(get_pool),
    currency: str | None = Query(default=None),
) -> dict:
    """2026-10-03's Dashboard page - from/to are required (the page
    always has a period selected, e.g. "This Month"; there's no
    all-time default the way some /reports endpoints have). currency
    defaults to the caller's own users.default_currency, same KEY
    PRODUCT RULE every other report follows - this never sums or
    converts across currencies, it's a filtered view of exactly one.
    """
    _require_pool(pool)

    try:
        resolved_currency = await resolve_dashboard_currency(pool, user_id=user_id, currency=currency)
        dashboard = await get_dashboard(
            pool, user_id=user_id, currency=resolved_currency, from_date=from_date, to_date=to_date
        )
    except HTTPException:
        raise
    except Exception:
        logger.error("Unexpected error building dashboard for user %s", user_id, exc_info=True)
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail=GENERIC_ERROR_MESSAGE,
        ) from None

    return dashboard
