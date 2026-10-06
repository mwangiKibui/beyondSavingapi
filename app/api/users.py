import logging
from datetime import datetime
from uuid import UUID

import asyncpg
import bcrypt
from fastapi import APIRouter, Depends, HTTPException, status
from pydantic import BaseModel, EmailStr, Field, field_validator

from app.core.db import get_pool
from app.core.errors import GENERIC_ERROR_MESSAGE
from app.core.security import AuthContext, get_auth_context, get_current_user_id
from app.repositories.users import (
    EmailAlreadyExists,
    IncorrectPassword,
    get_user_by_id,
    update_user_password,
    update_user_preferences,
    update_user_profile,
)

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/users", tags=["users"])


class ImpersonationInfo(BaseModel):
    admin_id: UUID
    admin_email: str
    read_only: bool


class UserProfileResponse(BaseModel):
    id: UUID
    email: str
    first_name: str
    last_name: str
    default_currency: str
    near_threshold: float
    created_at: datetime
    role: str
    impersonating: ImpersonationInfo | None = None


class UpdateProfileRequest(BaseModel):
    # A full profile replace, not a partial update - all three fields are
    # required on every call, matching the frontend's single "Save
    # profile" form submission.
    first_name: str = Field(min_length=1)
    last_name: str = Field(min_length=1)
    email: EmailStr


class ChangePasswordRequest(BaseModel):
    current_password: str = Field(min_length=1)
    # Same length bounds as signup/reset - required, and capped at
    # bcrypt's 72-byte limit.
    new_password: str = Field(min_length=8, max_length=72)


class ChangePasswordResponse(BaseModel):
    ok: bool = True


class UpdatePreferencesRequest(BaseModel):
    # Matches the users.default_currency column's CHAR(3).
    default_currency: str = Field(min_length=3, max_length=3)
    # Matches the users.near_threshold column's CHECK (> 0 AND < 1) -
    # validated here so a bad value 422s cleanly instead of hitting the DB
    # constraint.
    near_threshold: float = Field(gt=0, lt=1)

    @field_validator("default_currency")
    @classmethod
    def _uppercase_currency(cls, value: str) -> str:
        # Currency codes are conventionally uppercase (KES, USD, ...).
        return value.upper()


def _require_pool(pool: asyncpg.Pool | None) -> None:
    if pool is None:
        logger.error("Database pool unavailable")
        raise HTTPException(status_code=status.HTTP_503_SERVICE_UNAVAILABLE, detail=GENERIC_ERROR_MESSAGE)


@router.get("/me", response_model=UserProfileResponse)
async def get_me(
    ctx: AuthContext = Depends(get_auth_context),
    pool: asyncpg.Pool | None = Depends(get_pool),
) -> dict:
    # Deliberately uses get_auth_context, not get_current_user_id, so a
    # read-only impersonation session can still load its own profile (to
    # render the impersonation banner) without tripping the write-blocking
    # check - viewing this isn't "acting on the impersonated user's data".
    _require_pool(pool)

    try:
        user = await get_user_by_id(pool, ctx.user_id)
        if user is None:
            raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="User not found")

        if ctx.impersonated_by is not None:
            admin = await get_user_by_id(pool, ctx.impersonated_by)
            if admin is not None:
                user["impersonating"] = {
                    "admin_id": admin["id"],
                    "admin_email": admin["email"],
                    "read_only": ctx.read_only,
                }
    except HTTPException:
        raise
    except Exception:
        logger.error("Unexpected error fetching profile for user %s", ctx.user_id, exc_info=True)
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR, detail=GENERIC_ERROR_MESSAGE
        ) from None

    return user


@router.patch("/me", response_model=UserProfileResponse)
async def update_profile(
    payload: UpdateProfileRequest,
    user_id: UUID = Depends(get_current_user_id),
    pool: asyncpg.Pool | None = Depends(get_pool),
) -> dict:
    _require_pool(pool)

    try:
        user = await update_user_profile(
            pool,
            user_id=user_id,
            first_name=payload.first_name,
            last_name=payload.last_name,
            email=payload.email,
        )
        if user is None:
            raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="User not found")
    except HTTPException:
        raise
    except EmailAlreadyExists as exc:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail="That email is already in use by another account",
        ) from exc
    except Exception:
        logger.error("Unexpected error updating profile for user %s", user_id, exc_info=True)
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR, detail=GENERIC_ERROR_MESSAGE
        ) from None

    return user


@router.patch("/me/password", response_model=ChangePasswordResponse)
async def change_password(
    payload: ChangePasswordRequest,
    user_id: UUID = Depends(get_current_user_id),
    pool: asyncpg.Pool | None = Depends(get_pool),
) -> ChangePasswordResponse:
    _require_pool(pool)

    new_password_hash = bcrypt.hashpw(payload.new_password.encode(), bcrypt.gensalt()).decode()

    try:
        found = await update_user_password(
            pool,
            user_id=user_id,
            current_password=payload.current_password,
            new_password_hash=new_password_hash,
        )
        if not found:
            raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="User not found")
    except HTTPException:
        raise
    except IncorrectPassword as exc:
        # Never reveal anything more specific than "wrong password".
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED, detail="Current password is incorrect"
        ) from exc
    except Exception:
        logger.error("Unexpected error changing password for user %s", user_id, exc_info=True)
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR, detail=GENERIC_ERROR_MESSAGE
        ) from None

    return ChangePasswordResponse()


@router.patch("/me/preferences", response_model=UserProfileResponse)
async def update_preferences(
    payload: UpdatePreferencesRequest,
    user_id: UUID = Depends(get_current_user_id),
    pool: asyncpg.Pool | None = Depends(get_pool),
) -> dict:
    _require_pool(pool)

    try:
        user = await update_user_preferences(
            pool,
            user_id=user_id,
            default_currency=payload.default_currency,
            near_threshold=payload.near_threshold,
        )
        if user is None:
            raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="User not found")
    except HTTPException:
        raise
    except Exception:
        logger.error("Unexpected error updating preferences for user %s", user_id, exc_info=True)
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR, detail=GENERIC_ERROR_MESSAGE
        ) from None

    return user
