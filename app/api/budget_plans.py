import logging
from datetime import datetime
from decimal import Decimal
from typing import Literal
from uuid import UUID

import asyncpg
from fastapi import APIRouter, Depends, HTTPException, status
from pydantic import BaseModel, Field, model_validator

from app.core.db import get_pool
from app.core.errors import GENERIC_ERROR_MESSAGE
from app.core.security import get_current_user_id
from app.repositories.budget_plans import (
    DuplicateBudget,
    add_budget,
    create_budget_plan,
    list_budget_plans,
    list_plan_money_flows,
    update_budget,
)
from app.repositories.categories import get_categories_by_ids

logger = logging.getLogger(__name__)

router = APIRouter(tags=["budget-plans"])

Period = Literal["daily", "weekly", "monthly", "annual"]


class CreateBudgetPlanRequest(BaseModel):
    name: str | None = None
    period: Period
    starts_at: datetime
    ends_at: datetime
    total_cap: Decimal | None = Field(default=None, ge=0)
    is_recurring: bool = False

    @model_validator(mode="after")
    def check_window(self) -> "CreateBudgetPlanRequest":
        if self.ends_at <= self.starts_at:
            raise ValueError("ends_at must be after starts_at")
        return self


class BudgetPlanResponse(BaseModel):
    id: UUID
    name: str | None
    period: str
    starts_at: datetime
    ends_at: datetime
    total_cap: Decimal | None
    currency: str
    is_recurring: bool
    is_active: bool
    created_at: datetime


class AddBudgetRequest(BaseModel):
    category_id: UUID
    limit_amount: Decimal = Field(ge=0)


class BudgetResponse(BaseModel):
    id: UUID
    plan_id: UUID
    category_id: UUID
    limit_amount: Decimal
    created_at: datetime


class UpdateBudgetRequest(BaseModel):
    limit_amount: Decimal = Field(ge=0)


class BudgetWithStateResponse(BaseModel):
    id: UUID
    category_id: UUID
    category_name: str
    category_type: str
    limit_amount: Decimal
    consumed: Decimal
    remaining: Decimal
    percent: Decimal
    state: str


class BudgetPlanListItem(BudgetPlanResponse):
    budgets: list[BudgetWithStateResponse]
    total_consumed: Decimal | None
    total_state: str | None


class BudgetPlanListResponse(BaseModel):
    items: list[BudgetPlanListItem]


class MoneyFlowItem(BaseModel):
    id: UUID
    txn_date: datetime
    counterparty: str | None
    description: str | None
    category_name: str | None
    amount: Decimal
    currency: str


class MoneyFlowsResponse(BaseModel):
    money_in: list[MoneyFlowItem]
    money_out: list[MoneyFlowItem]


def _require_pool(pool: asyncpg.Pool | None) -> None:
    if pool is None:
        logger.error("Database pool unavailable")
        raise HTTPException(status_code=status.HTTP_503_SERVICE_UNAVAILABLE, detail=GENERIC_ERROR_MESSAGE)


@router.post("/budget-plans", response_model=BudgetPlanResponse, status_code=status.HTTP_201_CREATED)
async def create_budget_plan_endpoint(
    payload: CreateBudgetPlanRequest,
    user_id: UUID = Depends(get_current_user_id),
    pool: asyncpg.Pool | None = Depends(get_pool),
) -> dict:
    _require_pool(pool)

    try:
        plan = await create_budget_plan(
            pool,
            user_id=user_id,
            name=payload.name,
            period=payload.period,
            starts_at=payload.starts_at,
            ends_at=payload.ends_at,
            total_cap=payload.total_cap,
            is_recurring=payload.is_recurring,
        )
    except Exception:
        logger.error("Unexpected error creating budget plan for user %s", user_id, exc_info=True)
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR, detail=GENERIC_ERROR_MESSAGE
        ) from None

    return plan


@router.post(
    "/budget-plans/{plan_id}/budgets", response_model=BudgetResponse, status_code=status.HTTP_201_CREATED
)
async def add_budget_endpoint(
    plan_id: UUID,
    payload: AddBudgetRequest,
    user_id: UUID = Depends(get_current_user_id),
    pool: asyncpg.Pool | None = Depends(get_pool),
) -> dict:
    _require_pool(pool)

    try:
        found = await get_categories_by_ids(pool, category_ids=[payload.category_id], user_id=user_id)
        if not found:
            raise HTTPException(status_code=status.HTTP_422_UNPROCESSABLE_ENTITY, detail="Category not found")

        budget = await add_budget(
            pool,
            user_id=user_id,
            plan_id=plan_id,
            category_id=payload.category_id,
            limit_amount=payload.limit_amount,
        )
        if budget is None:
            raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Budget plan not found")
    except HTTPException:
        raise
    except DuplicateBudget as exc:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail="A budget for that category already exists in this plan",
        ) from exc
    except Exception:
        logger.error("Unexpected error adding budget to plan %s for user %s", plan_id, user_id, exc_info=True)
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR, detail=GENERIC_ERROR_MESSAGE
        ) from None

    return budget


@router.patch("/budgets/{budget_id}", response_model=BudgetResponse)
async def update_budget_endpoint(
    budget_id: UUID,
    payload: UpdateBudgetRequest,
    user_id: UUID = Depends(get_current_user_id),
    pool: asyncpg.Pool | None = Depends(get_pool),
) -> dict:
    _require_pool(pool)

    try:
        budget = await update_budget(pool, user_id=user_id, budget_id=budget_id, limit_amount=payload.limit_amount)
        if budget is None:
            raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Budget not found")
    except HTTPException:
        raise
    except Exception:
        logger.error("Unexpected error updating budget %s for user %s", budget_id, user_id, exc_info=True)
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR, detail=GENERIC_ERROR_MESSAGE
        ) from None

    return budget


@router.get("/budget-plans", response_model=BudgetPlanListResponse)
async def list_budget_plans_endpoint(
    user_id: UUID = Depends(get_current_user_id),
    pool: asyncpg.Pool | None = Depends(get_pool),
) -> dict:
    _require_pool(pool)

    try:
        plans = await list_budget_plans(pool, user_id=user_id)
    except Exception:
        logger.error("Unexpected error listing budget plans for user %s", user_id, exc_info=True)
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR, detail=GENERIC_ERROR_MESSAGE
        ) from None

    return {"items": plans}


@router.get("/budget-plans/{plan_id}/money-flows", response_model=MoneyFlowsResponse)
async def get_plan_money_flows_endpoint(
    plan_id: UUID,
    user_id: UUID = Depends(get_current_user_id),
    pool: asyncpg.Pool | None = Depends(get_pool),
) -> dict:
    _require_pool(pool)

    try:
        flows = await list_plan_money_flows(pool, user_id=user_id, plan_id=plan_id)
        if flows is None:
            raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Budget plan not found")
    except HTTPException:
        raise
    except Exception:
        logger.error(
            "Unexpected error fetching money flows for plan %s for user %s", plan_id, user_id, exc_info=True
        )
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR, detail=GENERIC_ERROR_MESSAGE
        ) from None

    return flows
