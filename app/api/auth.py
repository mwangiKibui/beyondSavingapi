from datetime import datetime, timedelta, timezone
from uuid import UUID

import asyncpg
import bcrypt
import jwt
from fastapi import APIRouter, Depends, HTTPException, status
from pydantic import BaseModel, EmailStr, Field

from app.core.config import get_settings
from app.core.db import get_pool
from app.repositories.users import EmailAlreadyExists, create_user, get_user_by_email

router = APIRouter(prefix="/auth", tags=["auth"])


class SignUpRequest(BaseModel):
    first_name: str = Field(min_length=1)
    last_name: str = Field(min_length=1)
    email: EmailStr
    # bcrypt silently truncates beyond 72 bytes, so reject anything longer
    # up front rather than let it silently misbehave.
    password: str = Field(min_length=8, max_length=72)


class UserResponse(BaseModel):
    id: UUID
    email: str
    first_name: str
    last_name: str
    default_currency: str
    near_threshold: float
    created_at: datetime


@router.post("/signup", response_model=UserResponse, status_code=status.HTTP_201_CREATED)
async def signup(
    payload: SignUpRequest, pool: asyncpg.Pool | None = Depends(get_pool)
) -> dict:
    if pool is None:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE, detail="Database unavailable"
        )

    password_hash = bcrypt.hashpw(payload.password.encode(), bcrypt.gensalt()).decode()

    try:
        user = await create_user(
            pool,
            email=payload.email,
            first_name=payload.first_name,
            last_name=payload.last_name,
            password_hash=password_hash,
        )
    except EmailAlreadyExists as exc:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT, detail="Email already registered"
        ) from exc

    return user


class LoginRequest(BaseModel):
    email: EmailStr
    password: str


class TokenResponse(BaseModel):
    access_token: str
    token_type: str = "bearer"


@router.post("/login", response_model=TokenResponse)
async def login(
    payload: LoginRequest, pool: asyncpg.Pool | None = Depends(get_pool)
) -> TokenResponse:
    if pool is None:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE, detail="Database unavailable"
        )

    invalid_credentials = HTTPException(
        status_code=status.HTTP_401_UNAUTHORIZED, detail="Invalid email or password"
    )

    user = await get_user_by_email(pool, payload.email)
    if user is None or not bcrypt.checkpw(
        payload.password.encode(), user["password_hash"].encode()
    ):
        # Same error either way - never reveal whether the email is registered.
        raise invalid_credentials

    settings = get_settings()
    expires_at = datetime.now(timezone.utc) + timedelta(minutes=settings.jwt_expires_minutes)
    token = jwt.encode(
        {"sub": str(user["id"]), "exp": expires_at},
        settings.jwt_secret,
        algorithm=settings.jwt_algorithm,
    )

    return TokenResponse(access_token=token)
