import logging
from datetime import datetime
from typing import Literal
from uuid import UUID

import asyncpg
from fastapi import APIRouter, Depends, HTTPException, status
from pydantic import BaseModel, Field

from app.core.db import get_pool
from app.core.security import get_current_user_id
from app.repositories.accounts import DuplicateAccount, create_account

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
