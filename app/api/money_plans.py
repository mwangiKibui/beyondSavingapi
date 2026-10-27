import logging
from datetime import datetime
from decimal import Decimal
from typing import Literal
from uuid import UUID

import asyncpg
from fastapi import APIRouter, Depends, HTTPException, Query, status
from pydantic import BaseModel, field_validator, model_validator

from app.core.db import get_pool
from app.core.errors import GENERIC_ERROR_MESSAGE
from app.core.security import get_current_user_id
from app.repositories.money_plans import create_money_plan, list_money_plans

logger = logging.getLogger(__name__)

router = APIRouter(tags=["money-plans"])

Period = Literal["daily", "weekly", "monthly", "annual", "custom"]
PlanStatus = Literal["draft", "converted", "archived"]


class CreateMoneyPlanRequest(BaseModel):
    # The plan's optional alias, e.g. "November salary".
    name: str | None = None
    period: Period
    starts_at: datetime
    ends_at: datetime

    @field_validator("name")
    @classmethod
    def _blank_name_is_none(cls, value: str | None) -> str | None:
        if value is None:
            return None
        return value.strip() or None

    @model_validator(mode="after")
    def check_window(self) -> "CreateMoneyPlanRequest":
        if self.ends_at <= self.starts_at:
            raise ValueError("ends_at must be after starts_at")
        return self


class MoneyPlanResponse(BaseModel):
    id: UUID
    name: str | None
    period: str
    starts_at: datetime
    ends_at: datetime
    currency: str
    status: str
    converted_at: datetime | None
    created_at: datetime
    updated_at: datetime


class MoneyPlanSummary(BaseModel):
    income: Decimal
    spending: Decimal
    loan_repayments: Decimal
    savings: Decimal
    left_over: Decimal
    will_save: Decimal
    save_rate: Decimal | None


class MoneyPlanListItem(MoneyPlanResponse):
    entry_count: int
    summary: MoneyPlanSummary


class MoneyPlanListResponse(BaseModel):
    items: list[MoneyPlanListItem]


def _require_pool(pool: asyncpg.Pool | None) -> None:
    if pool is None:
        logger.error("Database pool unavailable")
        raise HTTPException(status_code=status.HTTP_503_SERVICE_UNAVAILABLE, detail=GENERIC_ERROR_MESSAGE)


@router.post("/money-plans", response_model=MoneyPlanResponse, status_code=status.HTTP_201_CREATED)
async def create_money_plan_endpoint(
    payload: CreateMoneyPlanRequest,
    user_id: UUID = Depends(get_current_user_id),
    pool: asyncpg.Pool | None = Depends(get_pool),
) -> dict:
    _require_pool(pool)

    try:
        plan = await create_money_plan(
            pool,
            user_id=user_id,
            name=payload.name,
            period=payload.period,
            starts_at=payload.starts_at,
            ends_at=payload.ends_at,
        )
    except Exception:
        logger.error("Unexpected error creating money plan for user %s", user_id, exc_info=True)
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR, detail=GENERIC_ERROR_MESSAGE
        ) from None

    return plan


@router.get("/money-plans", response_model=MoneyPlanListResponse)
async def list_money_plans_endpoint(
    status_filter: PlanStatus | None = Query(default=None, alias="status"),
    user_id: UUID = Depends(get_current_user_id),
    pool: asyncpg.Pool | None = Depends(get_pool),
) -> dict:
    _require_pool(pool)

    try:
        plans = await list_money_plans(pool, user_id=user_id, status=status_filter)
    except Exception:
        logger.error("Unexpected error listing money plans for user %s", user_id, exc_info=True)
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR, detail=GENERIC_ERROR_MESSAGE
        ) from None

    return {"items": plans}
