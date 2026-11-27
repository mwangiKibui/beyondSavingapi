"""ab-317: admin "Statement layouts" - each institution's layouts (retire,
restore, edit) and the queue of uploads waiting for support, which an admin
maps with the wizard's own steps.

An admin sees only the waiting upload's page and the rows read from it -
never who uploaded it or any of their other transactions.
"""

import logging
from datetime import datetime
from typing import Literal
from uuid import UUID

import asyncpg
from fastapi import APIRouter, Depends, HTTPException, Query, status
from pydantic import BaseModel

from app.api.statement_layouts import (
    LayoutCheckResponse,
    LayoutRequest,
    LayoutSavedResponse,
    LayoutStartResponse,
    check_mapping,
    save_mapping,
    start_layout,
)
from app.core.db import get_pool
from app.core.errors import GENERIC_ERROR_MESSAGE
from app.core.security import require_admin
from app.repositories.statement_imports import get_waiting_import, list_awaiting_support
from app.repositories.statement_profiles import get_profile, list_institution_profiles
from app.services.admin_statement_layouts import LayoutAdminError, edit_layout, restore_layout, retire_layout

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/admin", tags=["admin"])

Source = Literal["builtin", "ai", "user", "admin"]
Status = Literal["draft", "active", "retired"]


class LastCheck(BaseModel):
    result: Literal["passed", "failed"]
    at: datetime


class LayoutSummary(BaseModel):
    id: UUID
    institution_id: UUID
    fingerprint: str
    version: int
    source: Source
    status: Status
    # Uploads read with it (each passed its balance check).
    usage_count: int
    last_check: LastCheck | None
    created_at: datetime | None = None
    updated_at: datetime | None = None


class LayoutDetail(LayoutSummary):
    profile: dict


class LayoutList(BaseModel):
    items: list[LayoutSummary]


class EditLayoutRequest(BaseModel):
    profile: dict


class WaitingUpload(BaseModel):
    id: UUID
    institution_id: UUID | None
    institution_name: str | None
    provider: str
    uploaded_at: datetime
    file_type: Literal["pdf", "csv", "xlsx"]
    fingerprint: str | None


class WaitingList(BaseModel):
    items: list[WaitingUpload]
    total: int
    page: int
    page_size: int


def last_check(row: dict) -> dict | None:
    """The latest balance-check result for a layout."""
    passed, failed = row.get("last_check_passed_at"), row.get("last_check_failed_at")
    if passed is None and failed is None:
        return None
    if failed is not None and (passed is None or failed > passed):
        return {"result": "failed", "at": failed}
    return {"result": "passed", "at": passed}


def _summary(row: dict) -> dict:
    return {**row, "last_check": last_check(row)}


def _require_pool(pool) -> None:
    if pool is None:
        logger.error("Database pool unavailable")
        raise HTTPException(status_code=status.HTTP_503_SERVICE_UNAVAILABLE, detail=GENERIC_ERROR_MESSAGE)


def _unexpected(action: str, target: UUID | None, admin_id: UUID) -> HTTPException:
    logger.error("Unexpected error %s (statement layout %s) for admin %s", action, target, admin_id, exc_info=True)
    return HTTPException(status_code=status.HTTP_500_INTERNAL_SERVER_ERROR, detail=GENERIC_ERROR_MESSAGE)


# --- layouts ------------------------------------------------------------------------


@router.get("/institutions/{institution_id}/statement-layouts", response_model=LayoutList)
async def list_layouts_endpoint(
    institution_id: UUID,
    admin_id: UUID = Depends(require_admin),
    pool: asyncpg.Pool | None = Depends(get_pool),
) -> dict:
    _require_pool(pool)
    try:
        rows = await list_institution_profiles(pool, institution_id=institution_id)
    except Exception:
        raise _unexpected("listing", institution_id, admin_id) from None
    return {"items": [_summary(r) for r in rows]}


@router.get("/statement-layouts/{profile_id}", response_model=LayoutDetail)
async def get_layout_endpoint(
    profile_id: UUID,
    admin_id: UUID = Depends(require_admin),
    pool: asyncpg.Pool | None = Depends(get_pool),
) -> dict:
    _require_pool(pool)
    try:
        row = await get_profile(pool, profile_id=profile_id)
    except Exception:
        raise _unexpected("reading", profile_id, admin_id) from None
    if row is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Statement layout not found")
    return _summary(row)


async def _run(action: str, profile_id: UUID, admin_id: UUID, call):
    try:
        return await call()
    except LayoutAdminError as e:
        raise HTTPException(status_code=e.status_code, detail=e.detail) from None
    except HTTPException:
        raise
    except Exception:
        raise _unexpected(action, profile_id, admin_id) from None


async def _detail(pool, profile_id: UUID, admin_id: UUID) -> dict:
    return await _run("reading", profile_id, admin_id, lambda: get_profile(pool, profile_id=profile_id))


@router.post("/statement-layouts/{profile_id}/retire", response_model=LayoutDetail)
async def retire_layout_endpoint(
    profile_id: UUID,
    admin_id: UUID = Depends(require_admin),
    pool: asyncpg.Pool | None = Depends(get_pool),
) -> dict:
    _require_pool(pool)
    await _run("retiring", profile_id, admin_id, lambda: retire_layout(pool, profile_id=profile_id))
    logger.info("Admin %s retired statement layout %s", admin_id, profile_id)
    return _summary(await _detail(pool, profile_id, admin_id))


@router.post("/statement-layouts/{profile_id}/restore", response_model=LayoutDetail)
async def restore_layout_endpoint(
    profile_id: UUID,
    admin_id: UUID = Depends(require_admin),
    pool: asyncpg.Pool | None = Depends(get_pool),
) -> dict:
    _require_pool(pool)
    await _run("restoring", profile_id, admin_id, lambda: restore_layout(pool, profile_id=profile_id))
    logger.info("Admin %s restored statement layout %s", admin_id, profile_id)
    return _summary(await _detail(pool, profile_id, admin_id))


@router.put("/statement-layouts/{profile_id}", response_model=LayoutDetail)
async def edit_layout_endpoint(
    profile_id: UUID,
    body: EditLayoutRequest,
    admin_id: UUID = Depends(require_admin),
    pool: asyncpg.Pool | None = Depends(get_pool),
) -> dict:
    """Stores the edited settings as the layout's next version (by an admin)."""
    _require_pool(pool)
    created = await _run(
        "editing", profile_id, admin_id,
        lambda: edit_layout(pool, profile_id=profile_id, profile=body.profile, admin_id=admin_id),
    )
    logger.info("Admin %s edited statement layout %s as %s v%s", admin_id, profile_id, created["id"],
                created.get("version"))
    return _summary(created)


# --- uploads waiting for support ---------------------------------------------------------


@router.get("/statement-imports/awaiting-support", response_model=WaitingList)
async def list_waiting_endpoint(
    page: int = Query(1, ge=1),
    page_size: int = Query(20, ge=1, le=100),
    admin_id: UUID = Depends(require_admin),
    pool: asyncpg.Pool | None = Depends(get_pool),
) -> dict:
    _require_pool(pool)
    try:
        items, total = await list_awaiting_support(pool, limit=page_size, offset=(page - 1) * page_size)
    except Exception:
        raise _unexpected("listing waiting uploads", None, admin_id) from None
    return {"items": items, "total": total, "page": page, "page_size": page_size}


async def _waiting(pool, import_id: UUID) -> dict:
    statement_import = await get_waiting_import(pool, import_id=import_id)
    if statement_import is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="No upload waiting for support with that id")
    return statement_import


@router.get("/statement-imports/{import_id}/layout", response_model=LayoutStartResponse)
async def start_waiting_layout_endpoint(
    import_id: UUID,
    admin_id: UUID = Depends(require_admin),
    pool: asyncpg.Pool | None = Depends(get_pool),
) -> dict:
    _require_pool(pool)
    return await _run("starting a waiting upload's layout", import_id, admin_id,
                      lambda: _start(pool, import_id))


async def _start(pool, import_id: UUID) -> dict:
    return await start_layout(pool, await _waiting(pool, import_id))


async def _check(pool, import_id: UUID, body: LayoutRequest) -> dict:
    return await check_mapping(await _waiting(pool, import_id), body)


async def _save(pool, import_id: UUID, body: LayoutRequest, admin_id: UUID) -> dict:
    created, _ = await save_mapping(pool, await _waiting(pool, import_id), body, actor_id=admin_id, source="admin")
    return created


@router.post("/statement-imports/{import_id}/layout/check", response_model=LayoutCheckResponse)
async def check_waiting_layout_endpoint(
    import_id: UUID,
    body: LayoutRequest,
    admin_id: UUID = Depends(require_admin),
    pool: asyncpg.Pool | None = Depends(get_pool),
) -> dict:
    _require_pool(pool)
    return await _run("checking a waiting upload's layout", import_id, admin_id,
                      lambda: _check(pool, import_id, body))


@router.put("/statement-imports/{import_id}/layout", response_model=LayoutSavedResponse)
async def save_waiting_layout_endpoint(
    import_id: UUID,
    body: LayoutRequest,
    admin_id: UUID = Depends(require_admin),
    pool: asyncpg.Pool | None = Depends(get_pool),
) -> dict:
    """Saves the admin's mapping as a shared layout and re-reads the upload
    (and the institution's other waiting uploads)."""
    _require_pool(pool)
    created = await _run("saving a waiting upload's layout", import_id, admin_id,
                         lambda: _save(pool, import_id, body, admin_id))
    logger.info("Admin %s saved layout %s for waiting upload %s", admin_id, created["id"], import_id)
    return {"status": "pending", "profile_id": created["id"]}
