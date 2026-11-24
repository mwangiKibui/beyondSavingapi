"""GET /search (ab-274): the navbar search over the signed-in user's own
transactions, payees, money plans and accounts. Pages and actions are a
fixed list in the UI (ab-275). There is no loans table yet, so loans is
always an empty group - the shape won't change when it lands.

Without `type`: every group, each capped at `limit`, with its total (the
dropdown). With `type`: that group only, paged (the full results page)."""

import logging
from datetime import date
from typing import Literal
from uuid import UUID

import asyncpg
from fastapi import APIRouter, Depends, HTTPException, Query, status
from pydantic import BaseModel

from app.core.config import get_settings
from app.core.db import get_pool
from app.core.errors import GENERIC_ERROR_MESSAGE
from app.core.security import get_current_user_id
from app.repositories.search import search_accounts, search_money_plans, search_payee_rows, search_transactions
from app.services.search import (
    MAX_QUERY_LENGTH,
    MIN_QUERY_LENGTH,
    account_item,
    like_pattern,
    normalise_query,
    parse_amount,
    payee_items,
    plan_item,
    transaction_item,
)

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/search", tags=["search"])

GroupType = Literal["transactions", "payees", "plans", "loans", "accounts"]
GROUP_TYPES: tuple[str, ...] = ("transactions", "payees", "plans", "loans", "accounts")
ALLOWED_PAGE_SIZES = (5, 10, 20, 30)
DEFAULT_PAGE_SIZE = 10


class SearchItem(BaseModel):
    type: Literal["transaction", "payee", "plan", "loan", "account"]
    id: str
    title: str
    subtitle: str | None = None
    # Ids the UI builds its link from (transaction_id + account_id; group_key
    # + direction + search; plan_id; account_id).
    link: dict[str, str]
    # Transactions
    txn_date: date | None = None
    amount: float | None = None
    currency: str | None = None
    direction: str | None = None
    account_nickname: str | None = None
    # Payees
    is_person: bool | None = None
    row_count: int | None = None


class SearchGroup(BaseModel):
    items: list[SearchItem]
    total: int


class SearchResponse(BaseModel):
    q: str
    groups: dict[str, SearchGroup]


@router.get("", response_model=SearchResponse, response_model_exclude_none=True)
async def search_endpoint(
    q: str = Query(max_length=MAX_QUERY_LENGTH),
    limit: int = Query(default=5, ge=1, le=20),
    group_type: GroupType | None = Query(default=None, alias="type"),
    page: int = Query(default=1, ge=1),
    page_size: int = Query(default=DEFAULT_PAGE_SIZE),
    user_id: UUID = Depends(get_current_user_id),
    pool: asyncpg.Pool | None = Depends(get_pool),
) -> dict:
    text = normalise_query(q)
    if text is None:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail=f"q must be at least {MIN_QUERY_LENGTH} characters",
        )
    if page_size not in ALLOWED_PAGE_SIZES:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail=f"page_size must be one of {ALLOWED_PAGE_SIZES}",
        )
    if group_type is None:
        wanted, size, offset = GROUP_TYPES, limit, 0
    else:
        wanted, size, offset = (group_type,), page_size, (page - 1) * page_size

    if pool is None:
        logger.error("Database pool unavailable")
        raise HTTPException(status_code=status.HTTP_503_SERVICE_UNAVAILABLE, detail=GENERIC_ERROR_MESSAGE)

    pattern = like_pattern(text)
    paging = {"user_id": user_id, "pattern": pattern, "limit": size, "offset": offset}
    groups: dict[str, dict] = {}
    try:
        async with pool.acquire() as conn:
            for name in wanted:
                if name == "transactions":
                    rows, total = await search_transactions(conn, amount=parse_amount(text), **paging)
                    items = [transaction_item(r) for r in rows]
                elif name == "payees":
                    rows = await search_payee_rows(conn, user_id=user_id, pattern=pattern)
                    items, total = payee_items(rows, q=text, secret=get_settings().suggestion_key_secret,
                                               limit=size, offset=offset)
                elif name == "plans":
                    rows, total = await search_money_plans(conn, **paging)
                    items = [plan_item(r) for r in rows]
                elif name == "accounts":
                    rows, total = await search_accounts(conn, **paging)
                    items = [account_item(r) for r in rows]
                else:  # loans: no table yet
                    items, total = [], 0
                groups[name] = {"items": items, "total": total}
    except Exception:
        logger.error("Unexpected error searching for user %s", user_id, exc_info=True)
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR, detail=GENERIC_ERROR_MESSAGE
        ) from None

    return {"q": text, "groups": groups}
