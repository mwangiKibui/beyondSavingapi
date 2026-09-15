from datetime import datetime
from uuid import UUID

import asyncpg
import bcrypt
from fastapi import APIRouter, Depends, HTTPException, status
from pydantic import BaseModel, EmailStr, Field

from app.core.db import get_pool
from app.repositories.users import EmailAlreadyExists, create_user

router = APIRouter(prefix="/auth", tags=["auth"])


class SignUpRequest(BaseModel):
    name: str = Field(min_length=1)
    email: EmailStr
    # bcrypt silently truncates beyond 72 bytes, so reject anything longer
    # up front rather than let it silently misbehave.
    password: str = Field(min_length=8, max_length=72)


class UserResponse(BaseModel):
    id: UUID
    email: str
    name: str | None
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
            pool, email=payload.email, name=payload.name, password_hash=password_hash
        )
    except EmailAlreadyExists as exc:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT, detail="Email already registered"
        ) from exc

    return user
