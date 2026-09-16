import logging
from datetime import datetime
from typing import Literal
from uuid import UUID

import asyncpg
from fastapi import APIRouter, Depends, HTTPException, Query, status
from pydantic import BaseModel, Field

from app.core.db import get_pool
from app.core.security import get_current_user_id
from app.repositories.accounts import DuplicateAccount, create_account, list_accounts

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/accounts", tags=["accounts"])

AccountType = Literal["bank", "mobile_money", "sacco"]
Currency = Literal["KES", "USD", "EUR", "GBP", "UGX", "TZS"]

# MVP1 supports a fixed set of institutions per account type - keep in
# sync with beyondSavingUI's PROVIDERS_BY_TYPE (src/lib/accountFormatting.ts).
PROVIDERS_BY_TYPE: dict[str, list[str]] = {
    "bank": ["Equity Bank", "NCBA Bank"],
    "mobile_money": ["M-Pesa", "Airtel Money"],
    "sacco": ["Mentor Sacco", "Biashara Sacco"],
}

MOBILE_NUMBER_LENGTH = 9
MAX_ACCOUNT_NUMBER_LENGTH = 20


class CreateAccountRequest(BaseModel):
    nickname: str = Field(min_length=1)
    account_type: AccountType
    provider: str = Field(min_length=1)
    account_number: str = Field(min_length=1)
    currency: Currency


class AccountResponse(BaseModel):
    id: UUID
    nickname: str
    account_type: str
    provider: str
    account_number: str
    currency: str
    is_active: bool
    created_at: datetime
    updated_at: datetime


ALLOWED_PAGE_SIZES = (5, 10, 20, 30)
SortBy = Literal["nickname", "provider", "currency", "balance", "unreconciled_count"]
SortDir = Literal["asc", "desc"]
ReconciliationStatus = Literal["reconciled", "unreconciled"]


class AccountListItem(BaseModel):
    id: UUID
    nickname: str
    account_type: str
    provider: str
    account_number: str
    currency: str
    balance: float
    unreconciled_count: int


class AccountListResponse(BaseModel):
    items: list[AccountListItem]
    total: int
    page: int
    page_size: int


def _validate_provider(account_type: str, provider: str) -> None:
    if provider not in PROVIDERS_BY_TYPE[account_type]:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail=f"'{provider}' is not a supported provider for {account_type} accounts",
        )


def _validate_account_number(account_type: str, account_number: str) -> None:
    if not account_number.isdigit():
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail="Account number must contain only digits",
        )

    if account_type == "mobile_money":
        if len(account_number) != MOBILE_NUMBER_LENGTH:
            raise HTTPException(
                status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
                detail=f"Mobile number must be exactly {MOBILE_NUMBER_LENGTH} digits",
            )
    elif len(account_number) > MAX_ACCOUNT_NUMBER_LENGTH:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail=f"Account number must be at most {MAX_ACCOUNT_NUMBER_LENGTH} digits",
        )


@router.post("", response_model=AccountResponse, status_code=status.HTTP_201_CREATED)
async def create_account_endpoint(
    payload: CreateAccountRequest,
    user_id: UUID = Depends(get_current_user_id),
    pool: asyncpg.Pool | None = Depends(get_pool),
) -> dict:
    if pool is None:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE, detail="Database unavailable"
        )

    try:
        _validate_provider(payload.account_type, payload.provider)
        _validate_account_number(payload.account_type, payload.account_number)

        account = await create_account(
            pool,
            user_id=user_id,
            nickname=payload.nickname,
            account_type=payload.account_type,
            provider=payload.provider,
            account_number=payload.account_number,
            currency=payload.currency,
        )
    except HTTPException:
        raise
    except DuplicateAccount as exc:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail="An account with that number already exists",
        ) from exc
    except Exception:
        logger.error("Unexpected error creating account for user %s", user_id, exc_info=True)
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail="Something went wrong. Please try again.",
        ) from None

    return account


@router.get("", response_model=AccountListResponse)
async def list_accounts_endpoint(
    user_id: UUID = Depends(get_current_user_id),
    pool: asyncpg.Pool | None = Depends(get_pool),
    page: int = Query(default=1, ge=1),
    page_size: int = Query(default=10),
    search: str | None = Query(default=None, min_length=1),
    sort_by: SortBy = "nickname",
    sort_dir: SortDir = "asc",
    account_type: AccountType | None = None,
    currency: Currency | None = None,
    reconciliation_status: ReconciliationStatus | None = None,
) -> dict:
    # Literal[5, 10, 20, 30] doesn't reliably coerce a query string ("20")
    # against int literals in Pydantic v2, so this is validated manually
    # rather than via the type annotation - matches this endpoint's own
    # provider/account_number checks (ab-23), not a one-off exception.
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
        items, total = await list_accounts(
            pool,
            user_id=user_id,
            page=page,
            page_size=page_size,
            search=search,
            sort_by=sort_by,
            sort_dir=sort_dir,
            account_type=account_type,
            currency=currency,
            reconciliation_status=reconciliation_status,
        )
    except Exception:
        logger.error("Unexpected error listing accounts for user %s", user_id, exc_info=True)
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail="Something went wrong. Please try again.",
        ) from None

    return {"items": items, "total": total, "page": page, "page_size": page_size}
