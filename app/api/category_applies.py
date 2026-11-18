"""Apply a category to payee groups, saved straight away (ab-282), from an
import's review or from Transactions -> Uncategorised; undo one apply; and
file a remembered payee's rows still waiting from earlier statements."""

import logging
from typing import Literal
from uuid import UUID

import asyncpg
from fastapi import APIRouter, Depends, HTTPException, status
from pydantic import BaseModel, Field, field_validator

from app.core.config import get_settings
from app.core.db import get_pool
from app.core.errors import GENERIC_ERROR_MESSAGE
from app.core.security import get_current_user_id
from app.api.accounts import AccountType, Currency, _validate_account_number, _validate_provider
from app.repositories.category_suggestions import get_import_for_preview
from app.repositories.notifications import generate_budget_alerts
from app.services.allocations import AllocationRejected, alert_groups
from app.services.category_applies import (
    ApplyGroup,
    ApplyResult,
    apply_earlier,
    apply_groups,
    reopen_lines,
    undo_apply,
)

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/category-applies", tags=["category-applies"])

NOTE_MAX = 200


class NewAccountRequest(BaseModel):
    """ab-285: an account added from the transfer drawer - the same checks
    as POST /accounts, except the number may be left out."""
    nickname: str = Field(min_length=1, max_length=60)
    account_type: AccountType
    provider: str = Field(min_length=1)
    account_number: str = ""
    currency: Currency | None = None

    @field_validator("nickname", "account_number", mode="before")
    @classmethod
    def _strip(cls, value):
        return value.strip() if isinstance(value, str) else value


class ApplyGroupRequest(BaseModel):
    # The group's rows, as listed; all must be the same payee and still
    # uncategorised.
    transaction_ids: list[UUID] = Field(min_length=1, max_length=5000)
    # Exactly one of these four (checked in the handler).
    category_id: UUID | None = None
    new_category_name: str | None = None
    use_suggestion: bool = False
    # ab-285: file as a Funds transfer under this reason.
    transfer_reason_id: UUID | None = None
    # ab-288: what the money is. Saving and Borrowed take no category or
    # reason; a transfer with no reason files under "Between my accounts".
    kind: Literal["expense", "income", "refund", "transfer", "saving", "borrowed", "loan_repayment"] | None = None
    # ab-285, transfers only - where the money came from (money in) or went
    # to (money out); at most one. someone_else_name is private to the user.
    other_account_id: UUID | None = None
    new_account: NewAccountRequest | None = None
    someone_else_name: str | None = None
    # ab-286: money in filed as a refund of an expense category (with
    # category_id or new_category_name). Refunds are never remembered.
    refund: bool = False
    # "From now on": the payee's top saved choice, plus a shared vote for
    # business payees. Off learns nothing.
    remember: bool = False
    # ab-292: one line of a payee filed on its own - remember learns softly
    # (one more use, never forced to the top) and casts no shared vote.
    per_line: bool = False
    # ab-288: what the rows were, in the user's words - mainly for "Other".
    # Saved on each allocation (shown on the Transaction statement and
    # Funds transfers reports); private, never learned or shared.
    note: str | None = None

    @field_validator("new_category_name", "someone_else_name", "note", mode="before")
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

    @field_validator("someone_else_name")
    @classmethod
    def _someone_length(cls, value):
        if value is not None and len(value) > 60:
            raise ValueError("Name must be 60 characters or fewer")
        return value

    @field_validator("note")
    @classmethod
    def _note_length(cls, value):
        if value is not None and len(value) > NOTE_MAX:
            raise ValueError(f"Note must be {NOTE_MAX} characters or fewer")
        return value


class ApplyRequest(BaseModel):
    # Set when applying from that import's review (its "Decided" tab).
    import_id: UUID | None = None
    groups: list[ApplyGroupRequest] = Field(min_length=1, max_length=500)


class AppliedGroup(BaseModel):
    apply_id: UUID
    group_key: str
    # A category, or for a Funds transfer (ab-285) a transfer reason.
    category_id: UUID | None = None
    category_name: str | None = None
    transfer_reason_id: UUID | None = None
    transfer_reason_name: str | None = None
    other_account_id: UUID | None = None
    is_refund: bool = False
    kind: str | None = None
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


class CreatedAccount(BaseModel):
    id: UUID
    nickname: str
    account_type: str
    provider: str
    currency: str


class ApplyResponse(BaseModel):
    groups: list[AppliedGroup]
    created_categories: list[CreatedCategory]
    created_accounts: list[CreatedAccount] = []


class ReopenRequest(BaseModel):
    transaction_ids: list[UUID] = Field(min_length=1, max_length=5000)


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
    return {
        "groups": result.groups,
        "created_categories": result.created_categories,
        "created_accounts": result.created_accounts,
    }


@router.post("", response_model=ApplyResponse, status_code=status.HTTP_201_CREATED)
async def apply_categories_endpoint(
    body: ApplyRequest,
    user_id: UUID = Depends(get_current_user_id),
    pool: asyncpg.Pool | None = Depends(get_pool),
) -> dict:
    for group in body.groups:
        chosen = sum([group.category_id is not None, group.new_category_name is not None, group.use_suggestion,
                      group.transfer_reason_id is not None])
        # ab-288: these kinds need no target (or have a default one).
        targetless = group.kind in ("saving", "borrowed") or (group.kind == "transfer" and chosen == 0)
        if group.kind == "refund":
            group.refund = True
        if chosen != 1 and not targetless:
            raise HTTPException(
                status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
                detail=(
                    "Each group needs exactly one of category_id, new_category_name, use_suggestion "
                    "or transfer_reason_id"
                ),
            )
        if group.refund and (group.category_id is None and group.new_category_name is None):
            raise HTTPException(
                status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
                detail="A refund needs the expense category it comes off",
            )
        if group.refund and group.remember:
            raise HTTPException(status_code=status.HTTP_422_UNPROCESSABLE_ENTITY, detail="Refunds aren't remembered")
        if group.new_account is not None:
            _validate_provider(group.new_account.account_type, group.new_account.provider)
            if group.new_account.account_number:
                _validate_account_number(group.new_account.account_type, group.new_account.account_number)
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
                reopened = await undo_apply(
                    conn, user_id=user_id, apply_id=apply_id, secret=get_settings().suggestion_key_secret
                )
    except AllocationRejected as exc:
        raise HTTPException(status_code=exc.status_code, detail=exc.detail) from None
    except Exception:
        logger.error("Unexpected error undoing apply %s for user %s", apply_id, user_id, exc_info=True)
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR, detail=GENERIC_ERROR_MESSAGE
        ) from None
    return {"reopened": reopened}


@router.post("/{apply_id}/reopen", response_model=UndoResponse)
async def reopen_lines_endpoint(
    apply_id: UUID,
    body: ReopenRequest,
    user_id: UUID = Depends(get_current_user_id),
    pool: asyncpg.Pool | None = Depends(get_pool),
) -> dict:
    """ab-292: puts some lines of a choice back to open (to file one line
    differently) without undoing the rest."""
    _require_pool(pool)
    try:
        async with pool.acquire() as conn:
            async with conn.transaction():
                reopened = await reopen_lines(
                    conn, user_id=user_id, apply_id=apply_id, transaction_ids=body.transaction_ids,
                    secret=get_settings().suggestion_key_secret,
                )
    except AllocationRejected as exc:
        raise HTTPException(status_code=exc.status_code, detail=exc.detail) from None
    except Exception:
        logger.error("Unexpected error reopening lines of apply %s for user %s", apply_id, user_id, exc_info=True)
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
