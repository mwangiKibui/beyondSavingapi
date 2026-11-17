"""Apply a category to payee groups, saved straight away (ab-282), from an
import's review or from Transactions -> Uncategorised; undo one apply; and
file a remembered payee's rows still waiting from earlier statements."""

import logging
from uuid import UUID

import asyncpg
from fastapi import APIRouter, Depends, HTTPException, status
from pydantic import BaseModel, Field, field_validator

from app.core.config import get_settings
from app.core.db import get_pool
from app.core.errors import GENERIC_ERROR_MESSAGE
from app.core.security import get_current_user_id
from app.repositories.category_suggestions import get_import_for_preview
from app.repositories.notifications import generate_budget_alerts
from app.services.allocations import AllocationRejected, alert_groups
from app.services.category_applies import ApplyGroup, ApplyResult, apply_earlier, apply_groups, undo_apply

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/category-applies", tags=["category-applies"])


class ApplyGroupRequest(BaseModel):
    # The group's rows, as listed; all must be the same payee and still
    # uncategorised.
    transaction_ids: list[UUID] = Field(min_length=1, max_length=5000)
    # Exactly one of these three (checked in the handler).
    category_id: UUID | None = None
    new_category_name: str | None = None
    use_suggestion: bool = False
    # "From now on": the payee's top saved choice, plus a shared vote for
    # business payees. Off learns nothing.
    remember: bool = False

    @field_validator("new_category_name", mode="before")
    @classmethod
    def _strip(cls, value):
        if isinstance(value, str):
            value = value.strip()
            return value or None
        return value

    @field_validator("new_category_name")
    @classmethod
    def _name_length(cls, value):
        if value is not None and len(value) > 60:
            raise ValueError("Category name must be 60 characters or fewer")
        return value


class ApplyRequest(BaseModel):
    # Set when applying from that import's review (its "Decided" tab).
    import_id: UUID | None = None
    groups: list[ApplyGroupRequest] = Field(min_length=1, max_length=500)


class AppliedGroup(BaseModel):
    apply_id: UUID
    group_key: str
    category_id: UUID
    category_name: str
    remember: bool
    allocated: list[UUID]
    # Zero-amount rows: nothing to allocate.
    skipped: list[UUID]
    # Remembered payees: rows still waiting in Uncategorised from earlier
    # statements - POST /category-applies/{apply_id}/earlier files them.
    earlier_waiting: int


class CreatedCategory(BaseModel):
    id: UUID
    name: str
    type: str


class ApplyResponse(BaseModel):
    groups: list[AppliedGroup]
    created_categories: list[CreatedCategory]


class UndoResponse(BaseModel):
    # Rows back to uncategorised.
    reopened: list[UUID]


def _require_pool(pool: asyncpg.Pool | None) -> None:
    if pool is None:
        logger.error("Database pool unavailable")
        raise HTTPException(status_code=status.HTTP_503_SERVICE_UNAVAILABLE, detail=GENERIC_ERROR_MESSAGE)


async def _alerts(pool: asyncpg.Pool, *, user_id: UUID, result: ApplyResult) -> None:
    """After commit, as confirm and bulk-allocate do: a failure is logged
    and never undoes the saved categories."""
    try:
        for (category_id, txn_date, currency), amount in alert_groups(
            result.plan, result.transactions_by_id
        ).items():
            await generate_budget_alerts(
                pool, user_id=user_id, category_id=category_id, txn_date=txn_date, currency=currency,
                new_amount=amount,
            )
    except Exception:
        logger.error("Unexpected error generating budget alerts after an apply, user %s", user_id, exc_info=True)


def _response(result: ApplyResult) -> dict:
    return {"groups": result.groups, "created_categories": result.created_categories}


@router.post("", response_model=ApplyResponse, status_code=status.HTTP_201_CREATED)
async def apply_categories_endpoint(
    body: ApplyRequest,
    user_id: UUID = Depends(get_current_user_id),
    pool: asyncpg.Pool | None = Depends(get_pool),
) -> dict:
    for group in body.groups:
        chosen = sum([group.category_id is not None, group.new_category_name is not None, group.use_suggestion])
        if chosen != 1:
            raise HTTPException(
                status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
                detail="Each group needs exactly one of category_id, new_category_name or use_suggestion",
            )
    _require_pool(pool)

    try:
        statement_import = None
        if body.import_id is not None:
            statement_import = await get_import_for_preview(pool, import_id=body.import_id, user_id=user_id)
            if statement_import is None:
                raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Statement import not found")
            if statement_import["status"] != "parsed":
                raise HTTPException(
                    status_code=status.HTTP_409_CONFLICT, detail="This statement hasn't finished processing yet"
                )
        async with pool.acquire() as conn:
            async with conn.transaction():
                result = await apply_groups(
                    conn,
                    user_id=user_id,
                    groups=[ApplyGroup(**g.model_dump()) for g in body.groups],
                    secret=get_settings().suggestion_key_secret,
                    statement_import=statement_import,
                )
    except AllocationRejected as exc:
        raise HTTPException(status_code=exc.status_code, detail=exc.detail) from None
    except HTTPException:
        raise
    except Exception:
        logger.error("Unexpected error applying categories for user %s", user_id, exc_info=True)
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR, detail=GENERIC_ERROR_MESSAGE
        ) from None

    await _alerts(pool, user_id=user_id, result=result)
    return _response(result)


@router.delete("/{apply_id}", response_model=UndoResponse)
async def undo_apply_endpoint(
    apply_id: UUID,
    user_id: UUID = Depends(get_current_user_id),
    pool: asyncpg.Pool | None = Depends(get_pool),
) -> dict:
    _require_pool(pool)
    try:
        async with pool.acquire() as conn:
            async with conn.transaction():
                reopened = await undo_apply(conn, user_id=user_id, apply_id=apply_id)
    except AllocationRejected as exc:
        raise HTTPException(status_code=exc.status_code, detail=exc.detail) from None
    except Exception:
        logger.error("Unexpected error undoing apply %s for user %s", apply_id, user_id, exc_info=True)
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR, detail=GENERIC_ERROR_MESSAGE
        ) from None
    return {"reopened": reopened}


@router.post("/{apply_id}/earlier", response_model=ApplyResponse)
async def apply_earlier_endpoint(
    apply_id: UUID,
    user_id: UUID = Depends(get_current_user_id),
    pool: asyncpg.Pool | None = Depends(get_pool),
) -> dict:
    _require_pool(pool)
    try:
        async with pool.acquire() as conn:
            async with conn.transaction():
                result = await apply_earlier(
                    conn, user_id=user_id, apply_id=apply_id, secret=get_settings().suggestion_key_secret
                )
    except AllocationRejected as exc:
        raise HTTPException(status_code=exc.status_code, detail=exc.detail) from None
    except Exception:
        logger.error("Unexpected error filing earlier rows for apply %s, user %s", apply_id, user_id, exc_info=True)
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR, detail=GENERIC_ERROR_MESSAGE
        ) from None

    await _alerts(pool, user_id=user_id, result=result)
    return _response(result)
