import logging
from datetime import datetime
from typing import Literal
from uuid import UUID

import asyncpg
from fastapi import APIRouter, Depends, HTTPException, status
from pydantic import BaseModel, Field

from app.core.db import get_pool
from app.core.security import get_current_user_id
from app.repositories.accounts import get_account
from app.repositories.sub_ledgers import (
    DuplicateSubLedger,
    create_sub_ledger,
    delete_sub_ledger,
    list_sub_ledgers,
    update_sub_ledger,
)

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/accounts/{account_id}/sub-ledgers", tags=["sub-ledgers"])

BalanceTreatment = Literal["addition", "deduction"]


class CreateSubLedgerRequest(BaseModel):
    name: str = Field(min_length=1)
    balance_treatment: BalanceTreatment


class UpdateSubLedgerRequest(BaseModel):
    name: str | None = Field(default=None, min_length=1)
    balance_treatment: BalanceTreatment | None = None


class SubLedgerResponse(BaseModel):
    id: UUID
    account_id: UUID
    name: str
    balance_treatment: str
    created_at: datetime
    updated_at: datetime


class SubLedgerListResponse(BaseModel):
    items: list[SubLedgerResponse]


async def _get_owned_account(pool: asyncpg.Pool, *, account_id: UUID, user_id: UUID) -> dict:
    account = await get_account(pool, account_id=account_id, user_id=user_id)
    if account is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Account not found")
    return account


@router.get("", response_model=SubLedgerListResponse)
async def list_sub_ledgers_endpoint(
    account_id: UUID,
    user_id: UUID = Depends(get_current_user_id),
    pool: asyncpg.Pool | None = Depends(get_pool),
) -> dict:
    if pool is None:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE, detail="Database unavailable"
        )

    try:
        await _get_owned_account(pool, account_id=account_id, user_id=user_id)
        items = await list_sub_ledgers(pool, account_id=account_id)
    except HTTPException:
        raise
    except Exception:
        logger.error("Unexpected error listing sub-ledgers for account %s", account_id, exc_info=True)
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail="Something went wrong. Please try again.",
        ) from None

    return {"items": items}


@router.post("", response_model=SubLedgerResponse, status_code=status.HTTP_201_CREATED)
async def create_sub_ledger_endpoint(
    account_id: UUID,
    payload: CreateSubLedgerRequest,
    user_id: UUID = Depends(get_current_user_id),
    pool: asyncpg.Pool | None = Depends(get_pool),
) -> dict:
    if pool is None:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE, detail="Database unavailable"
        )

    try:
        await _get_owned_account(pool, account_id=account_id, user_id=user_id)
        sub_ledger = await create_sub_ledger(
            pool, account_id=account_id, name=payload.name, balance_treatment=payload.balance_treatment
        )
    except HTTPException:
        raise
    except DuplicateSubLedger as exc:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail="A sub-ledger with that name already exists on this account",
        ) from exc
    except Exception:
        logger.error("Unexpected error creating sub-ledger for account %s", account_id, exc_info=True)
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail="Something went wrong. Please try again.",
        ) from None

    return sub_ledger


@router.patch("/{sub_ledger_id}", response_model=SubLedgerResponse)
async def update_sub_ledger_endpoint(
    account_id: UUID,
    sub_ledger_id: UUID,
    payload: UpdateSubLedgerRequest,
    user_id: UUID = Depends(get_current_user_id),
    pool: asyncpg.Pool | None = Depends(get_pool),
) -> dict:
    if pool is None:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE, detail="Database unavailable"
        )

    try:
        if payload.name is None and payload.balance_treatment is None:
            raise HTTPException(
                status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
                detail="At least one of name or balance_treatment must be provided",
            )

        await _get_owned_account(pool, account_id=account_id, user_id=user_id)
        sub_ledger = await update_sub_ledger(
            pool,
            sub_ledger_id=sub_ledger_id,
            account_id=account_id,
            name=payload.name,
            balance_treatment=payload.balance_treatment,
        )
        if sub_ledger is None:
            raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Sub-ledger not found")
    except HTTPException:
        raise
    except DuplicateSubLedger as exc:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail="A sub-ledger with that name already exists on this account",
        ) from exc
    except Exception:
        logger.error("Unexpected error updating sub-ledger %s", sub_ledger_id, exc_info=True)
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail="Something went wrong. Please try again.",
        ) from None

    return sub_ledger


@router.delete("/{sub_ledger_id}", status_code=status.HTTP_204_NO_CONTENT)
async def delete_sub_ledger_endpoint(
    account_id: UUID,
    sub_ledger_id: UUID,
    user_id: UUID = Depends(get_current_user_id),
    pool: asyncpg.Pool | None = Depends(get_pool),
) -> None:
    if pool is None:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE, detail="Database unavailable"
        )

    try:
        await _get_owned_account(pool, account_id=account_id, user_id=user_id)
        deleted = await delete_sub_ledger(pool, sub_ledger_id=sub_ledger_id, account_id=account_id)
        if not deleted:
            raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Sub-ledger not found")
    except HTTPException:
        raise
    except Exception:
        logger.error("Unexpected error deleting sub-ledger %s", sub_ledger_id, exc_info=True)
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail="Something went wrong. Please try again.",
        ) from None
