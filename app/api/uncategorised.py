"""Transactions -> Uncategorised (ab-282): every statement row the user
still has to categorise, from every statement and month, grouped by payee.
Rows of an import whose review is still open wait in that review instead.
ab-284: the badge count, and "Categorised here" (what the user filed from
the tab, each undoable)."""

import logging
from typing import Literal
from uuid import UUID

import asyncpg
from fastapi import APIRouter, Depends, HTTPException, Query, status
from pydantic import BaseModel

from app.api.payee_group_models import ListParams, PayeeGroup, check_page_size, list_params
from app.core.config import get_settings
from app.core.db import get_pool
from app.core.errors import GENERIC_ERROR_MESSAGE
from app.core.security import get_current_user_id
from app.repositories.payee_groups import count_uncategorised_rows
from app.services.category_applies import categorised_here_groups, uncategorised_groups
from app.services.payee_groups import select_groups

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/uncategorised", tags=["uncategorised"])


class UncategorisedGroupsResponse(BaseModel):
    items: list[PayeeGroup]
    # Groups after the filters, for paging.
    total: int
    page: int
    page_size: int
    # Every waiting row, whatever the filters - the Transactions badge.
    row_total: int


class UncategorisedCountResponse(BaseModel):
    row_total: int


def _require_pool(pool: asyncpg.Pool | None) -> None:
    if pool is None:
        logger.error("Database pool unavailable")
        raise HTTPException(status_code=status.HTTP_503_SERVICE_UNAVAILABLE, detail=GENERIC_ERROR_MESSAGE)


@router.get("/count", response_model=UncategorisedCountResponse)
async def uncategorised_count_endpoint(
    user_id: UUID = Depends(get_current_user_id),
    pool: asyncpg.Pool | None = Depends(get_pool),
) -> dict:
    """Rows waiting in Uncategorised - the sidebar badge (ab-284)."""
    _require_pool(pool)
    try:
        row_total = await count_uncategorised_rows(pool, user_id=user_id)
    except Exception:
        logger.error("Unexpected error counting uncategorised rows for user %s", user_id, exc_info=True)
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR, detail=GENERIC_ERROR_MESSAGE
        ) from None
    return {"row_total": row_total}


@router.get("/groups", response_model=UncategorisedGroupsResponse)
async def list_uncategorised_groups_endpoint(
    # ab-284: open = still to categorise; categorised = filed from this tab
    # and not undone ("Categorised here"), each with its apply_id.
    group_status: Literal["open", "categorised"] = Query(default="open", alias="status"),
    params: ListParams = Depends(list_params),
    user_id: UUID = Depends(get_current_user_id),
    pool: asyncpg.Pool | None = Depends(get_pool),
) -> dict:
    check_page_size(params)
    _require_pool(pool)
    secret = get_settings().suggestion_key_secret
    try:
        if group_status == "categorised":
            groups = await categorised_here_groups(pool, user_id=user_id, secret=secret)
            row_total = await count_uncategorised_rows(pool, user_id=user_id)
        else:
            groups, row_total = await uncategorised_groups(pool, user_id=user_id, secret=secret)
    except Exception:
        logger.error("Unexpected error listing uncategorised groups for user %s", user_id, exc_info=True)
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR, detail=GENERIC_ERROR_MESSAGE
        ) from None
    items, total = select_groups(groups, **params.model_dump())
    return {"items": items, "total": total, "page": params.page, "page_size": params.page_size,
            "row_total": row_total}
