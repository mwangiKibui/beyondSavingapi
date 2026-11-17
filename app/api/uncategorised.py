"""Transactions -> Uncategorised (ab-282): every statement row the user
still has to categorise, from every statement and month, grouped by payee.
Rows of an import whose review is still open wait in that review instead."""

import logging
from uuid import UUID

import asyncpg
from fastapi import APIRouter, Depends, HTTPException, status
from pydantic import BaseModel

from app.api.payee_group_models import ListParams, PayeeGroup, check_page_size, list_params
from app.core.config import get_settings
from app.core.db import get_pool
from app.core.errors import GENERIC_ERROR_MESSAGE
from app.core.security import get_current_user_id
from app.services.category_applies import uncategorised_groups
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


@router.get("/groups", response_model=UncategorisedGroupsResponse)
async def list_uncategorised_groups_endpoint(
    params: ListParams = Depends(list_params),
    user_id: UUID = Depends(get_current_user_id),
    pool: asyncpg.Pool | None = Depends(get_pool),
) -> dict:
    check_page_size(params)
    if pool is None:
        logger.error("Database pool unavailable")
        raise HTTPException(status_code=status.HTTP_503_SERVICE_UNAVAILABLE, detail=GENERIC_ERROR_MESSAGE)
    try:
        groups, row_total = await uncategorised_groups(
            pool, user_id=user_id, secret=get_settings().suggestion_key_secret
        )
    except Exception:
        logger.error("Unexpected error listing uncategorised groups for user %s", user_id, exc_info=True)
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR, detail=GENERIC_ERROR_MESSAGE
        ) from None
    items, total = select_groups(groups, **params.model_dump())
    return {"items": items, "total": total, "page": params.page, "page_size": params.page_size,
            "row_total": row_total}
