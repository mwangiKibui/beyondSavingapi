"""ab-309: admin view and edit of plan limits (migration 000035), so a
limit can change without a deploy. value null = unlimited. Only existing
plan_limits rows are edited; plans and keys are added by migration."""

import logging
from typing import Annotated, Literal
from uuid import UUID

import asyncpg
from fastapi import APIRouter, Depends, HTTPException, status
from pydantic import BaseModel, Field, StrictInt

from app.core.db import get_pool
from app.core.errors import GENERIC_ERROR_MESSAGE
from app.core.security import require_admin
from app.repositories.plans import list_plans_with_limits, plan_exists, update_plan_limit
from app.services.limits import LIMIT_KEYS

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/admin", tags=["admin"])

# FastAPI answers 422 for any other key (the known set lives in limits.py).
LimitKey = Literal[LIMIT_KEYS]  # type: ignore[valid-type]


class PlanLimit(BaseModel):
    limit_key: str
    value: int | None


class PlanResponse(BaseModel):
    id: UUID
    key: str
    name: str
    limits: list[PlanLimit]


class UpdateLimitRequest(BaseModel):
    # Required, but may be null (unlimited); never negative.
    value: Annotated[StrictInt | None, Field(ge=0)]


def _server_error(action: str, admin_id: UUID) -> HTTPException:
    logger.error("Unexpected error %s, admin %s", action, admin_id, exc_info=True)
    return HTTPException(status_code=status.HTTP_500_INTERNAL_SERVER_ERROR, detail=GENERIC_ERROR_MESSAGE)


@router.get("/plans", response_model=list[PlanResponse])
async def list_plans_endpoint(
    admin_id: UUID = Depends(require_admin),
    pool: asyncpg.Pool | None = Depends(get_pool),
) -> list[dict]:
    try:
        return await list_plans_with_limits(pool)
    except Exception:
        raise _server_error("listing plans", admin_id) from None


@router.patch("/plans/{plan_id}/limits/{limit_key}", response_model=PlanLimit)
async def update_plan_limit_endpoint(
    plan_id: UUID,
    limit_key: LimitKey,
    body: UpdateLimitRequest,
    admin_id: UUID = Depends(require_admin),
    pool: asyncpg.Pool | None = Depends(get_pool),
) -> dict:
    try:
        updated = await update_plan_limit(pool, plan_id=plan_id, limit_key=limit_key, value=body.value)
        if updated is None:
            found = await plan_exists(pool, plan_id=plan_id)
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND,
                detail="This plan has no such limit" if found else "Plan not found",
            )
    except HTTPException:
        raise
    except Exception:
        raise _server_error(f"updating limit {limit_key} of plan {plan_id}", admin_id) from None
    logger.info("Admin %s set %s on plan %s to %s", admin_id, limit_key, plan_id, body.value)
    return updated
