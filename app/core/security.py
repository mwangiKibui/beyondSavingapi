from typing import NamedTuple
from uuid import UUID

import asyncpg
import jwt
from fastapi import Depends, HTTPException, Request, status
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer

from app.core.config import get_settings
from app.core.db import get_pool
from app.core.errors import GENERIC_ERROR_MESSAGE
from app.repositories.users import get_user_by_id

bearer_scheme = HTTPBearer(auto_error=False)

_SAFE_METHODS = {"GET", "HEAD", "OPTIONS"}


class AuthContext(NamedTuple):
    user_id: UUID
    impersonated_by: UUID | None
    imp_session_id: UUID | None
    read_only: bool


async def get_auth_context(
    credentials: HTTPAuthorizationCredentials | None = Depends(bearer_scheme),
) -> AuthContext:
    unauthorized = HTTPException(
        status_code=status.HTTP_401_UNAUTHORIZED,
        detail="Invalid or missing authentication token",
        headers={"WWW-Authenticate": "Bearer"},
    )

    if credentials is None:
        raise unauthorized

    settings = get_settings()
    try:
        payload = jwt.decode(
            credentials.credentials, settings.jwt_secret, algorithms=[settings.jwt_algorithm]
        )
    except jwt.PyJWTError as exc:
        raise unauthorized from exc

    sub = payload.get("sub")
    if sub is None:
        raise unauthorized

    try:
        user_id = UUID(sub)
        impersonated_by = UUID(payload["impersonated_by"]) if payload.get("impersonated_by") else None
        imp_session_id = UUID(payload["imp_session_id"]) if payload.get("imp_session_id") else None
    except ValueError as exc:
        raise unauthorized from exc

    return AuthContext(
        user_id=user_id,
        impersonated_by=impersonated_by,
        imp_session_id=imp_session_id,
        read_only=bool(payload.get("read_only", False)),
    )


async def get_current_user_id(
    request: Request, ctx: AuthContext = Depends(get_auth_context)
) -> UUID:
    # The one enforcement point for a read-only impersonation session: every
    # existing resource router already depends on this function, so gating
    # mutating methods here applies everywhere with no per-router changes.
    # GET /users/me and POST /admin/impersonation/end intentionally bypass
    # this by depending on get_auth_context directly instead - neither is
    # "acting on the impersonated user's data".
    if ctx.read_only and request.method not in _SAFE_METHODS:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="This is a read-only support session and cannot make changes",
        )
    return ctx.user_id


async def require_admin(
    user_id: UUID = Depends(get_current_user_id),
    pool: asyncpg.Pool | None = Depends(get_pool),
) -> UUID:
    if pool is None:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE, detail=GENERIC_ERROR_MESSAGE
        )

    # Re-checks the DB rather than trusting a JWT claim, so demoting an
    # admin takes effect immediately rather than waiting for their token
    # to expire.
    user = await get_user_by_id(pool, user_id)
    if user is None or user["role"] not in ("admin", "super_admin"):
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="Admin access required")
    return user_id
