import logging
from datetime import date
from typing import Literal
from uuid import UUID

import asyncpg
from fastapi import APIRouter, Depends, HTTPException, Query, status
from pydantic import BaseModel, Field

from app.core.db import get_pool
from app.core.security import get_current_user_id
from app.repositories.accounts import get_account
from app.repositories.statement_imports import get_import_owned_by_user
from app.repositories.sub_ledgers import list_sub_ledgers
from app.repositories.transactions import insert_manual_transactions, list_transactions

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/transactions", tags=["transactions"])

ALLOWED_PAGE_SIZES = (5, 10, 20, 30)
Direction = Literal["in", "out"]
ReconciliationStatus = Literal["reconciled", "unreconciled"]
SortBy = Literal["txn_date", "amount"]
SortDir = Literal["asc", "desc"]


class TransactionListItem(BaseModel):
    id: UUID
    account_id: UUID
    txn_date: date
    amount: float
    currency: str
    direction: str
    counterparty: str | None = None
    description: str | None = None
    balance_after: float | None = None
    status: str
    import_id: UUID | None = None
    sub_ledger_id: UUID | None = None
    sub_ledger_name: str | None = None


class TransactionListResponse(BaseModel):
    items: list[TransactionListItem]
    total: int
    page: int
    page_size: int


@router.get("", response_model=TransactionListResponse)
async def list_transactions_endpoint(
    user_id: UUID = Depends(get_current_user_id),
    pool: asyncpg.Pool | None = Depends(get_pool),
    account_id: UUID | None = Query(default=None),
    from_date: date | None = Query(default=None, alias="from"),
    to_date: date | None = Query(default=None, alias="to"),
    direction: Direction | None = None,
    status_filter: ReconciliationStatus | None = Query(default=None, alias="status"),
    import_id: UUID | None = Query(default=None),
    search: str | None = Query(default=None, min_length=1),
    page: int = Query(default=1, ge=1),
    page_size: int = Query(default=10),
    sort_by: SortBy = "txn_date",
    sort_dir: SortDir = "desc",
) -> dict:
    # Literal[5, 10, 20, 30] doesn't reliably coerce a query string ("20")
    # against int literals in Pydantic v2, so this is validated manually
    # rather than via the type annotation - matches this repo's other
    # paginated list endpoints (ab-24/ab-87).
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
        if account_id is not None:
            account = await get_account(pool, account_id=account_id, user_id=user_id)
            if account is None:
                raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Account not found")

        if import_id is not None:
            owned_import = await get_import_owned_by_user(pool, import_id=import_id, user_id=user_id)
            if owned_import is None:
                raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Import not found")

        items, total = await list_transactions(
            pool,
            user_id=user_id,
            account_id=account_id,
            from_date=from_date,
            to_date=to_date,
            direction=direction,
            status=status_filter,
            import_id=import_id,
            search=search,
            page=page,
            page_size=page_size,
            sort_by=sort_by,
            sort_dir=sort_dir,
        )
    except HTTPException:
        raise
    except Exception:
        logger.error("Unexpected error listing transactions for user %s", user_id, exc_info=True)
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail="Something went wrong. Please try again.",
        ) from None

    return {"items": items, "total": total, "page": page, "page_size": page_size}


class TransactionCreateItem(BaseModel):
    account_id: UUID
    sub_ledger_id: UUID | None = None
    txn_date: date
    # Validated manually against ("in", "out") below rather than typed as
    # Literal - every business-rule failure in this batch (this included)
    # reports as the same "Row N: ..." string, matching ManualEntryTable's
    # (ab-45/ab-46) own client-side messages, rather than mixing that with
    # Pydantic's structured per-field error shape for some checks only.
    direction: str
    amount: float
    description: str | None = None


class CreateTransactionsRequest(BaseModel):
    transactions: list[TransactionCreateItem] = Field(min_length=1)


class CreateTransactionsResponse(BaseModel):
    items: list[TransactionListItem]


@router.post("/batch", response_model=CreateTransactionsResponse, status_code=status.HTTP_201_CREATED)
async def create_transactions_endpoint(
    body: CreateTransactionsRequest,
    user_id: UUID = Depends(get_current_user_id),
    pool: asyncpg.Pool | None = Depends(get_pool),
) -> dict:
    if pool is None:
        raise HTTPException(status_code=status.HTTP_503_SERVICE_UNAVAILABLE, detail="Database unavailable")

    try:
        accounts_by_id: dict[UUID, dict] = {}
        sub_ledgers_by_account: dict[UUID, list[dict]] = {}

        for index, item in enumerate(body.transactions, start=1):
            row_label = f"Row {index}"

            if item.account_id not in accounts_by_id:
                account = await get_account(pool, account_id=item.account_id, user_id=user_id)
                if account is None:
                    raise HTTPException(
                        status_code=status.HTTP_404_NOT_FOUND, detail=f"{row_label}: Account not found"
                    )
                accounts_by_id[item.account_id] = account
                sub_ledgers_by_account[item.account_id] = await list_sub_ledgers(
                    pool, account_id=item.account_id
                )

            sub_ledgers = sub_ledgers_by_account[item.account_id]
            if sub_ledgers:
                if item.sub_ledger_id is None:
                    raise HTTPException(
                        status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
                        detail=f"{row_label}: Sub-ledger is required",
                    )
                if not any(sl["id"] == item.sub_ledger_id for sl in sub_ledgers):
                    raise HTTPException(
                        status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
                        detail=f"{row_label}: Sub-ledger does not belong to this account",
                    )
            elif item.sub_ledger_id is not None:
                raise HTTPException(
                    status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
                    detail=f"{row_label}: This account has no sub-ledgers",
                )

            if item.txn_date > date.today():
                raise HTTPException(
                    status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
                    detail=f"{row_label}: Date cannot be in the future",
                )
            if item.direction not in ("in", "out"):
                raise HTTPException(
                    status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
                    detail=f"{row_label}: Direction must be 'in' or 'out'",
                )
            if item.amount <= 0:
                raise HTTPException(
                    status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
                    detail=f"{row_label}: Amount must be greater than zero",
                )

        created = await insert_manual_transactions(
            pool,
            account_ids=[item.account_id for item in body.transactions],
            sub_ledger_ids=[item.sub_ledger_id for item in body.transactions],
            txn_dates=[item.txn_date for item in body.transactions],
            amounts=[item.amount for item in body.transactions],
            currencies=[accounts_by_id[item.account_id]["currency"] for item in body.transactions],
            directions=[item.direction for item in body.transactions],
            descriptions=[item.description for item in body.transactions],
        )
    except HTTPException:
        raise
    except Exception:
        logger.error("Unexpected error creating manual transactions for user %s", user_id, exc_info=True)
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail="Something went wrong. Please try again.",
        ) from None

    sub_ledger_names = {
        sub_ledger["id"]: sub_ledger["name"]
        for sub_ledgers in sub_ledgers_by_account.values()
        for sub_ledger in sub_ledgers
    }
    items = [
        {
            **row,
            "sub_ledger_name": sub_ledger_names.get(row["sub_ledger_id"]),
            # A transaction this endpoint just created can't already have
            # an allocation - always unreconciled, no need to query.
            "status": "unreconciled",
        }
        for row in created
    ]
    return {"items": items}
