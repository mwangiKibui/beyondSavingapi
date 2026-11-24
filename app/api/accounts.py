import re
import logging
from datetime import date, datetime
from typing import Literal
from uuid import UUID

import asyncpg
from fastapi import APIRouter, Depends, HTTPException, Query, status
from pydantic import BaseModel, Field

from app.core.db import get_pool
from app.core.errors import GENERIC_ERROR_MESSAGE
from app.core.security import get_current_user_id
from app.repositories.accounts import (
    DuplicateAccount,
    create_account,
    deactivate_account,
    get_account,
    get_book_balance,
    get_currency_summary,
    list_accounts,
    update_account,
)
from app.repositories.institutions import provider_allowed
from app.services.limits import ACCOUNTS_MAX, enforce_limit

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/accounts", tags=["accounts"])

AccountType = Literal["bank", "mobile_money", "sacco", "money_market"]
Currency = Literal["KES", "USD", "EUR", "GBP", "UGX", "TZS"]

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
    # ab-294: from the provider's institutions row - whether statements
    # can be uploaded for this account.
    statements_uploadable: bool = False
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
    is_active: bool
    statements_uploadable: bool = False
    balance: float
    unreconciled_count: int


class AccountListResponse(BaseModel):
    items: list[AccountListItem]
    total: int
    page: int
    page_size: int


class CurrencySubtotal(BaseModel):
    currency: str
    account_count: int
    total: float


class AccountsSummaryResponse(BaseModel):
    subtotals: list[CurrencySubtotal]


class BookBalanceResponse(BaseModel):
    balance: float
    as_of: date


class UpdateAccountRequest(BaseModel):
    # All optional - a genuine partial update. Currency and balance stay
    # out of scope (ab-25): currency is fixed at creation, balance derives
    # from transactions. account_number IS editable (broadened per ab-114
    # follow-up) - a bank/SACCO account number, or a mobile money number
    # for mobile_money accounts.
    nickname: str | None = Field(default=None, min_length=1)
    account_type: AccountType | None = None
    provider: str | None = Field(default=None, min_length=1)
    account_number: str | None = Field(default=None, min_length=1)


async def _validate_provider(pool, account_type: str, provider: str, *, current: str | None = None) -> None:
    """ab-294: the provider must be an active institution of this type in
    the admin-managed table - or the account's current provider, so an
    account whose provider was since retired can still be edited."""
    if not await provider_allowed(pool, name=provider, type=account_type, current=current):
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail=f"'{provider}' is not a supported provider for {account_type} accounts",
        )


# ab-288: money market fund member numbers can carry letters (e.g. Etica).
MEMBER_NUMBER_PATTERN = re.compile(r"^[A-Za-z0-9/-]+$")
# 2026-11-18: a Kenyan mobile number as people type it - 0712345678,
# 254712345678, +254712345678 or 712345678 (spaces and dashes ignored).
# Saved as its 9 digits; Safaricom and Airtel numbers start 7 or 1.
KENYAN_MOBILE_PATTERN = re.compile(r"^(?:\+?254|0)?([17]\d{8})$")


def normalize_mobile_number(raw: str) -> str | None:
    """The 9 digits of a Kenyan mobile number, or None when it isn't one."""
    match = KENYAN_MOBILE_PATTERN.match(re.sub(r"[\s-]", "", raw))
    return match.group(1) if match else None


def _validate_account_number(account_type: str, account_number: str) -> str:
    """Checks the number for the account type and returns it as saved (a
    mobile number becomes its 9 digits)."""
    if account_type == "mobile_money":
        normalized = normalize_mobile_number(account_number)
        if normalized is None:
            raise HTTPException(
                status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
                detail="Enter a Kenyan mobile number, e.g. 0712345678 or +254712345678",
            )
        return normalized
    if account_type == "money_market":
        if not MEMBER_NUMBER_PATTERN.match(account_number):
            raise HTTPException(
                status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
                detail="Member number can use letters, digits, - and / only",
            )
        if len(account_number) > MAX_ACCOUNT_NUMBER_LENGTH:
            raise HTTPException(
                status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
                detail=f"Member number must be at most {MAX_ACCOUNT_NUMBER_LENGTH} characters",
            )
        return account_number
    if not account_number.isdigit():
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail="Account number must contain only digits",
        )

    if len(account_number) > MAX_ACCOUNT_NUMBER_LENGTH:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail=f"Account number must be at most {MAX_ACCOUNT_NUMBER_LENGTH} digits",
        )
    return account_number


@router.post("", response_model=AccountResponse, status_code=status.HTTP_201_CREATED)
async def create_account_endpoint(
    payload: CreateAccountRequest,
    user_id: UUID = Depends(get_current_user_id),
    pool: asyncpg.Pool | None = Depends(get_pool),
) -> dict:
    if pool is None:
        logger.error("Database pool unavailable")
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE, detail=GENERIC_ERROR_MESSAGE
        )

    try:
        await _validate_provider(pool, payload.account_type, payload.provider)
        account_number = _validate_account_number(payload.account_type, payload.account_number)
        # ab-309: raises the 429 at the plan's accounts_max (active accounts).
        await enforce_limit(pool, user_id, ACCOUNTS_MAX)

        account = await create_account(
            pool,
            user_id=user_id,
            nickname=payload.nickname,
            account_type=payload.account_type,
            provider=payload.provider,
            account_number=account_number,
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
            detail=GENERIC_ERROR_MESSAGE,
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
        logger.error("Database pool unavailable")
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE, detail=GENERIC_ERROR_MESSAGE
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
            detail=GENERIC_ERROR_MESSAGE,
        ) from None

    return {"items": items, "total": total, "page": page, "page_size": page_size}


@router.get("/summary", response_model=AccountsSummaryResponse)
async def accounts_summary_endpoint(
    user_id: UUID = Depends(get_current_user_id),
    pool: asyncpg.Pool | None = Depends(get_pool),
) -> dict:
    if pool is None:
        logger.error("Database pool unavailable")
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE, detail=GENERIC_ERROR_MESSAGE
        )

    try:
        subtotals = await get_currency_summary(pool, user_id=user_id)
    except Exception:
        logger.error("Unexpected error summarizing accounts for user %s", user_id, exc_info=True)
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail=GENERIC_ERROR_MESSAGE,
        ) from None

    return {"subtotals": subtotals}


@router.patch("/{account_id}", response_model=AccountResponse)
async def update_account_endpoint(
    account_id: UUID,
    payload: UpdateAccountRequest,
    user_id: UUID = Depends(get_current_user_id),
    pool: asyncpg.Pool | None = Depends(get_pool),
) -> dict:
    if pool is None:
        logger.error("Database pool unavailable")
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE, detail=GENERIC_ERROR_MESSAGE
        )

    try:
        if (
            payload.nickname is None
            and payload.account_type is None
            and payload.provider is None
            and payload.account_number is None
        ):
            raise HTTPException(
                status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
                detail="At least one of nickname, account_type, provider, or account_number must be provided",
            )

        if payload.account_type is not None and (payload.provider is None or payload.account_number is None):
            raise HTTPException(
                status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
                detail="provider and account_number are required when changing account_type",
            )

        existing = await get_account(pool, account_id=account_id, user_id=user_id)
        if existing is None:
            raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Account not found")

        effective_type = payload.account_type or existing["account_type"]

        if payload.provider is not None:
            same_type = effective_type == existing["account_type"]
            await _validate_provider(
                pool, effective_type, payload.provider, current=existing["provider"] if same_type else None
            )

        account_number = payload.account_number
        if account_number is not None:
            account_number = _validate_account_number(effective_type, account_number)

        account = await update_account(
            pool,
            account_id=account_id,
            user_id=user_id,
            nickname=payload.nickname,
            account_type=payload.account_type,
            provider=payload.provider,
            account_number=account_number,
        )
        if account is None:
            raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Account not found")
    except HTTPException:
        raise
    except DuplicateAccount as exc:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail="An account with that number already exists",
        ) from exc
    except Exception:
        logger.error("Unexpected error updating account %s for user %s", account_id, user_id, exc_info=True)
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail=GENERIC_ERROR_MESSAGE,
        ) from None

    return account


@router.post("/{account_id}/deactivate", response_model=AccountResponse)
async def deactivate_account_endpoint(
    account_id: UUID,
    user_id: UUID = Depends(get_current_user_id),
    pool: asyncpg.Pool | None = Depends(get_pool),
) -> dict:
    if pool is None:
        logger.error("Database pool unavailable")
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE, detail=GENERIC_ERROR_MESSAGE
        )

    try:
        # Idempotent - deactivating an already-inactive account just
        # re-returns it as-is rather than erroring, so a UI double-fire
        # (or retry) isn't a special case.
        account = await deactivate_account(pool, account_id=account_id, user_id=user_id)
        if account is None:
            raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Account not found")
    except HTTPException:
        raise
    except Exception:
        logger.error("Unexpected error deactivating account %s for user %s", account_id, user_id, exc_info=True)
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail=GENERIC_ERROR_MESSAGE,
        ) from None

    return account


@router.get("/{account_id}/book-balance", response_model=BookBalanceResponse)
async def book_balance_endpoint(
    account_id: UUID,
    user_id: UUID = Depends(get_current_user_id),
    pool: asyncpg.Pool | None = Depends(get_pool),
    as_of: date = Query(default_factory=date.today),
) -> dict:
    if pool is None:
        logger.error("Database pool unavailable")
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE, detail=GENERIC_ERROR_MESSAGE
        )

    try:
        balance = await get_book_balance(pool, account_id=account_id, user_id=user_id, as_of=as_of)
        if balance is None:
            raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Account not found")
    except HTTPException:
        raise
    except Exception:
        logger.error(
            "Unexpected error computing book balance for account %s, user %s", account_id, user_id, exc_info=True
        )
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail=GENERIC_ERROR_MESSAGE,
        ) from None

    return {"balance": balance, "as_of": as_of}
