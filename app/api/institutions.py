"""ab-294: the providers the account pickers offer - every active
institution of the four account types, A-Z, straight from the table
admins manage (ab-197..199, CSV import). No paging: the lists are short.
"""

import logging
from uuid import UUID

import asyncpg
from fastapi import APIRouter, Depends, HTTPException, Query, status
from pydantic import BaseModel

from app.api.accounts import AccountType
from app.core.db import get_pool
from app.core.errors import GENERIC_ERROR_MESSAGE
from app.core.security import get_current_user_id
from app.repositories.institutions import list_active

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/institutions", tags=["institutions"])


class ProviderOut(BaseModel):
    id: UUID
    name: str
    type: str
    statements_uploadable: bool
    # ab-312: an active statement profile reads this provider's layout.
    recognised_layout: bool = False


class ProviderListResponse(BaseModel):
    items: list[ProviderOut]


@router.get("", response_model=ProviderListResponse)
async def list_providers_endpoint(
    account_type: AccountType | None = Query(default=None),
    user_id: UUID = Depends(get_current_user_id),
    pool: asyncpg.Pool | None = Depends(get_pool),
) -> dict:
    if pool is None:
        logger.error("Database pool unavailable")
        raise HTTPException(status_code=status.HTTP_503_SERVICE_UNAVAILABLE, detail=GENERIC_ERROR_MESSAGE)
    try:
        async with pool.acquire() as conn:
            items = await list_active(conn, type=account_type)
    except Exception:
        logger.error("Unexpected error listing providers for user %s", user_id, exc_info=True)
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR, detail=GENERIC_ERROR_MESSAGE
        ) from None
    # account_type=None still means the four account types only.
    items = [i for i in items if i["type"] in AccountType.__args__]
    return {"items": items}
