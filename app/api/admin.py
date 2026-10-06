import logging
from datetime import datetime, timedelta, timezone
from uuid import UUID

import asyncpg
import jwt
from fastapi import APIRouter, Depends, HTTPException, Query, status
from pydantic import BaseModel

from app.core.config import get_settings
from app.core.db import get_pool
from app.core.errors import GENERIC_ERROR_MESSAGE
from app.core.security import AuthContext, get_auth_context, require_admin
from app.repositories.admin_metrics import get_admin_metrics
from app.repositories.impersonation_audit import (
    create_impersonation_session,
    end_impersonation_session,
)
from app.repositories.users import get_user_by_id, list_users

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/admin", tags=["admin"])


def _require_pool(pool: asyncpg.Pool | None) -> None:
    if pool is None:
        logger.error("Database pool unavailable")
        raise HTTPException(status_code=status.HTTP_503_SERVICE_UNAVAILABLE, detail=GENERIC_ERROR_MESSAGE)


class AdminMetricsResponse(BaseModel):
    total_users: int
    total_accounts: int
    accounts_by_institution: list[dict]


@router.get("/metrics", response_model=AdminMetricsResponse)
async def get_metrics(
    admin_id: UUID = Depends(require_admin),
    pool: asyncpg.Pool | None = Depends(get_pool),
) -> dict:
    # require_admin already 503s when pool is None (it needs the pool
    # itself to re-check the caller's role), so pool is guaranteed here.
    return await get_admin_metrics(pool)


class AdminUserListItem(BaseModel):
    id: UUID
    email: str
    first_name: str
    last_name: str
    role: str
    created_at: datetime


class AdminUserListResponse(BaseModel):
    items: list[AdminUserListItem]
    total: int
    page: int
    page_size: int


@router.get("/users", response_model=AdminUserListResponse)
async def get_users(
    page: int = Query(1, ge=1),
    page_size: int = Query(10, ge=1, le=100),
    search: str | None = Query(None),
    admin_id: UUID = Depends(require_admin),
    pool: asyncpg.Pool | None = Depends(get_pool),
) -> dict:
    items, total = await list_users(pool, page=page, page_size=page_size, search=search)
    return {"items": items, "total": total, "page": page, "page_size": page_size}


class ImpersonateResponse(BaseModel):
    access_token: str
    token_type: str = "bearer"
    read_only: bool


@router.post("/users/{target_user_id}/impersonate", response_model=ImpersonateResponse)
async def impersonate_user(
    target_user_id: UUID,
    admin_id: UUID = Depends(require_admin),
    pool: asyncpg.Pool | None = Depends(get_pool),
) -> dict:
    # require_admin already confirmed admin_id belongs to an admin/
    # super_admin row - re-fetch just to read which tier, for read_only.
    admin = await get_user_by_id(pool, admin_id)

    target = await get_user_by_id(pool, target_user_id)
    if target is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="User not found")

    if target["role"] != "user":
        # No legitimate v1 support case for impersonating another admin,
        # and it closes off an easy privilege-chaining path.
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT, detail="Cannot impersonate another admin"
        )

    read_only = admin["role"] != "super_admin"
    session_id = await create_impersonation_session(
        pool, admin_id=admin_id, target_user_id=target_user_id
    )

    settings = get_settings()
    expires_at = datetime.now(timezone.utc) + timedelta(
        minutes=settings.jwt_impersonation_expires_minutes
    )
    token = jwt.encode(
        {
            "sub": str(target_user_id),
            "impersonated_by": str(admin_id),
            "imp_session_id": str(session_id),
            "read_only": read_only,
            "exp": expires_at,
        },
        settings.jwt_secret,
        algorithm=settings.jwt_algorithm,
    )

    return {"access_token": token, "read_only": read_only}


class EndImpersonationResponse(BaseModel):
    ok: bool = True


@router.post("/impersonation/end", response_model=EndImpersonationResponse)
async def end_impersonation(
    ctx: AuthContext = Depends(get_auth_context),
    pool: asyncpg.Pool | None = Depends(get_pool),
) -> EndImpersonationResponse:
    # Uses get_auth_context directly (not get_current_user_id /
    # require_admin) so this always works even for a read-only
    # impersonation session - exiting cleanly should never itself be
    # blocked by the read-only check.
    _require_pool(pool)

    if ctx.imp_session_id is None:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="This session is not an impersonation session",
        )

    await end_impersonation_session(pool, session_id=ctx.imp_session_id)
    return EndImpersonationResponse()
