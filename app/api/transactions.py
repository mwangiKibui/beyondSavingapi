import logging
from datetime import date, datetime
from typing import Literal
from uuid import UUID

import asyncpg
from fastapi import APIRouter, Depends, HTTPException, Query, status
from pydantic import BaseModel, Field

from app.core.db import get_pool
from app.core.errors import GENERIC_ERROR_MESSAGE
from app.core.security import get_current_user_id
from app.repositories.accounts import get_account, get_accounts_by_ids
from app.repositories.categories import get_categories_by_ids
from app.repositories.statement_imports import get_import_owned_by_user
from app.repositories.sub_ledgers import get_sub_ledgers_by_ids, list_sub_ledgers
from app.repositories.transactions import (
    get_allocations,
    get_transaction,
    get_transactions_with_allocated,
    insert_allocations,
    insert_bulk_allocations,
    insert_manual_transactions,
    list_transactions,
)
from app.repositories.transfer_reasons import get_transfer_reasons_by_ids

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/transactions", tags=["transactions"])

ALLOWED_PAGE_SIZES = (5, 10, 20, 30)
Direction = Literal["in", "out"]
ReconciliationStatus = Literal["reconciled", "partial", "unreconciled"]
# expense categories only ever apply to a money-out transaction, income
# only to money-in (ab-60) - category_id: null (reconciled, no category,
# ab-124) skips this check entirely, since there's no type to compare.
_CATEGORY_TYPE_FOR_DIRECTION = {"out": "expense", "in": "income"}
SortBy = Literal["txn_date", "amount"]
SortDir = Literal["asc", "desc"]


class TransactionListItem(BaseModel):
    id: UUID
    account_id: UUID
    txn_date: date
    amount: float
    currency: str
    direction: str
    counterparty: str | None = None
    description: str | None = None
    balance_after: float | None = None
    status: str
    import_id: UUID | None = None
    sub_ledger_id: UUID | None = None
    sub_ledger_name: str | None = None


class TransactionListResponse(BaseModel):
    items: list[TransactionListItem]
    total: int
    page: int
    page_size: int


@router.get("", response_model=TransactionListResponse)
async def list_transactions_endpoint(
    user_id: UUID = Depends(get_current_user_id),
    pool: asyncpg.Pool | None = Depends(get_pool),
    account_id: UUID | None = Query(default=None),
    from_date: date | None = Query(default=None, alias="from"),
    to_date: date | None = Query(default=None, alias="to"),
    direction: Direction | None = None,
    status_filter: ReconciliationStatus | None = Query(default=None, alias="status"),
    import_id: UUID | None = Query(default=None),
    search: str | None = Query(default=None, min_length=1),
    page: int = Query(default=1, ge=1),
    page_size: int = Query(default=10),
    sort_by: SortBy = "txn_date",
    sort_dir: SortDir = "desc",
) -> dict:
    # Literal[5, 10, 20, 30] doesn't reliably coerce a query string ("20")
    # against int literals in Pydantic v2, so this is validated manually
    # rather than via the type annotation - matches this repo's other
    # paginated list endpoints (ab-24/ab-87).
    if page_size not in ALLOWED_PAGE_SIZES:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail=f"page_size must be one of {ALLOWED_PAGE_SIZES}",
        )

    if pool is None:
        logger.error("Database pool unavailable")
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE, detail=GENERIC_ERROR_MESSAGE
        )

    try:
        if account_id is not None:
            account = await get_account(pool, account_id=account_id, user_id=user_id)
            if account is None:
                raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Account not found")

        if import_id is not None:
            owned_import = await get_import_owned_by_user(pool, import_id=import_id, user_id=user_id)
            if owned_import is None:
                raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Import not found")

        items, total = await list_transactions(
            pool,
            user_id=user_id,
            account_id=account_id,
            from_date=from_date,
            to_date=to_date,
            direction=direction,
            status=status_filter,
            import_id=import_id,
            search=search,
            page=page,
            page_size=page_size,
            sort_by=sort_by,
            sort_dir=sort_dir,
        )
    except HTTPException:
        raise
    except Exception:
        logger.error("Unexpected error listing transactions for user %s", user_id, exc_info=True)
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail=GENERIC_ERROR_MESSAGE,
        ) from None

    return {"items": items, "total": total, "page": page, "page_size": page_size}


class AllocationItem(BaseModel):
    id: UUID
    category_id: UUID | None = None
    category_name: str | None = None
    # Transfer-shaped split fields (ab-134) - all None for an ordinary
    # categorized/no-category allocation. Source names are resolved here
    # (not just ids) so the frontend can render a transfer-shaped split
    # without a second round-trip.
    transfer_reason_id: UUID | None = None
    transfer_reason_name: str | None = None
    source_account_id: UUID | None = None
    source_account_name: str | None = None
    source_sub_ledger_id: UUID | None = None
    source_sub_ledger_name: str | None = None
    source_description: str | None = None
    amount: float
    currency: str
    original_amount: float
    original_currency: str
    note: str | None = None
    created_at: datetime


class TransactionDetailResponse(TransactionListItem):
    allocations: list[AllocationItem]


@router.get("/{transaction_id}", response_model=TransactionDetailResponse)
async def get_transaction_endpoint(
    transaction_id: UUID,
    user_id: UUID = Depends(get_current_user_id),
    pool: asyncpg.Pool | None = Depends(get_pool),
) -> dict:
    if pool is None:
        logger.error("Database pool unavailable")
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE, detail=GENERIC_ERROR_MESSAGE
        )

    try:
        transaction = await get_transaction(pool, transaction_id=transaction_id, user_id=user_id)
        if transaction is None:
            raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Transaction not found")

        allocations = await get_allocations(pool, transaction_id=transaction_id)
    except HTTPException:
        raise
    except Exception:
        logger.error("Unexpected error fetching transaction %s for user %s", transaction_id, user_id, exc_info=True)
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail=GENERIC_ERROR_MESSAGE,
        ) from None

    return {**transaction, "allocations": allocations}


class AllocationCreateItem(BaseModel):
    category_id: UUID | None = None
    # Transfer-shaped split (ab-134) - exactly one of category_id or
    # transfer_reason_id may be set (both null is the existing reconciled-
    # no-category case). When transfer_reason_id is set, at most one of
    # the three source fields below may be meaningfully populated - a
    # source is one of a tracked account, a tracked sub-ledger, or a
    # free-text description, never several at once.
    transfer_reason_id: UUID | None = None
    source_account_id: UUID | None = None
    source_sub_ledger_id: UUID | None = None
    source_description: str | None = None
    amount: float
    note: str | None = None


class CreateAllocationsRequest(BaseModel):
    allocations: list[AllocationCreateItem] = Field(min_length=1)


@router.post(
    "/{transaction_id}/allocations",
    response_model=TransactionDetailResponse,
    status_code=status.HTTP_201_CREATED,
)
async def create_allocations_endpoint(
    transaction_id: UUID,
    body: CreateAllocationsRequest,
    user_id: UUID = Depends(get_current_user_id),
    pool: asyncpg.Pool | None = Depends(get_pool),
) -> dict:
    if pool is None:
        logger.error("Database pool unavailable")
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE, detail=GENERIC_ERROR_MESSAGE
        )

    try:
        transaction = await get_transaction(pool, transaction_id=transaction_id, user_id=user_id)
        if transaction is None:
            raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Transaction not found")

        for index, item in enumerate(body.allocations, start=1):
            if item.amount <= 0:
                raise HTTPException(
                    status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
                    detail=f"Split {index}: Amount must be greater than zero",
                )

        # ab-134: a transfer-shaped split (transfer_reason_id set) is an
        # alternative to a categorized one, never both - both null is
        # still the existing reconciled-no-category case. When it's
        # transfer-shaped, its source is exactly one of a tracked account,
        # a tracked sub-ledger, or a free-text description, never several.
        effective_source_descriptions: dict[int, str | None] = {}
        for index, item in enumerate(body.allocations, start=1):
            if item.category_id is not None and item.transfer_reason_id is not None:
                raise HTTPException(
                    status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
                    detail=f"Split {index}: Can't set both a category and a transfer reason",
                )
            source_description = (item.source_description or "").strip() or None
            effective_source_descriptions[index] = source_description
            if item.transfer_reason_id is None:
                continue
            sources_given = sum(
                1
                for value in (item.source_account_id, item.source_sub_ledger_id, source_description)
                if value is not None
            )
            if sources_given > 1:
                raise HTTPException(
                    status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
                    detail=(
                        f"Split {index}: A transfer's source is one of an account, a sub-ledger, "
                        "or a description - not several"
                    ),
                )

        category_ids = {item.category_id for item in body.allocations if item.category_id is not None}
        categories_by_id = {}
        if category_ids:
            found = await get_categories_by_ids(pool, category_ids=list(category_ids), user_id=user_id)
            categories_by_id = {category["id"]: category for category in found}
            missing = category_ids - categories_by_id.keys()
            if missing:
                raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Category not found")

        transfer_reason_ids = {
            item.transfer_reason_id for item in body.allocations if item.transfer_reason_id is not None
        }
        if transfer_reason_ids:
            found_reasons = await get_transfer_reasons_by_ids(
                pool, transfer_reason_ids=list(transfer_reason_ids), user_id=user_id
            )
            found_reason_ids = {reason["id"] for reason in found_reasons}
            missing_reasons = transfer_reason_ids - found_reason_ids
            if missing_reasons:
                raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Transfer reason not found")

        source_account_ids = {
            item.source_account_id for item in body.allocations if item.source_account_id is not None
        }
        if source_account_ids:
            found_accounts = await get_accounts_by_ids(
                pool, account_ids=list(source_account_ids), user_id=user_id
            )
            found_account_ids = {account["id"] for account in found_accounts}
            missing_accounts = source_account_ids - found_account_ids
            if missing_accounts:
                raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Source account not found")

        source_sub_ledger_ids = {
            item.source_sub_ledger_id for item in body.allocations if item.source_sub_ledger_id is not None
        }
        if source_sub_ledger_ids:
            found_sub_ledgers = await get_sub_ledgers_by_ids(
                pool, sub_ledger_ids=list(source_sub_ledger_ids), user_id=user_id
            )
            found_sub_ledger_ids = {sub_ledger["id"] for sub_ledger in found_sub_ledgers}
            missing_sub_ledgers = source_sub_ledger_ids - found_sub_ledger_ids
            if missing_sub_ledgers:
                raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Source sub-ledger not found")

        # ab-60: a category's type must match the transaction's direction -
        # skipped entirely for a null-category (reconciled, no category)
        # or transfer-shaped split, neither of which has a type to compare.
        expected_type = _CATEGORY_TYPE_FOR_DIRECTION[transaction["direction"]]
        for index, item in enumerate(body.allocations, start=1):
            if item.category_id is None:
                continue
            if categories_by_id[item.category_id]["type"] != expected_type:
                raise HTTPException(
                    status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
                    detail=f"Split {index}: Category type doesn't match this transaction's direction",
                )

        # ab-59: the sum of what's already allocated plus this request's
        # new splits can't exceed the transaction's own amount. Applies
        # identically to a transfer-shaped split's amount - it's the same
        # allocations row, just tagged with a reason instead of a category.
        existing_allocations = await get_allocations(pool, transaction_id=transaction_id)
        # asyncpg maps NUMERIC to Decimal - cast to float here since these
        # are compared/combined with Pydantic's plain float amounts below.
        already_allocated = sum(float(allocation["original_amount"]) for allocation in existing_allocations)
        new_total = sum(item.amount for item in body.allocations)
        transaction_amount = float(transaction["amount"])
        if already_allocated + new_total > transaction_amount:
            remaining = transaction_amount - already_allocated
            raise HTTPException(
                status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
                detail=f"Amount can't exceed the remaining {remaining:.2f} {transaction['currency']}",
            )

        await insert_allocations(
            pool,
            transaction_id=transaction_id,
            category_ids=[item.category_id for item in body.allocations],
            transfer_reason_ids=[item.transfer_reason_id for item in body.allocations],
            source_account_ids=[item.source_account_id for item in body.allocations],
            source_sub_ledger_ids=[item.source_sub_ledger_id for item in body.allocations],
            source_descriptions=[
                effective_source_descriptions[index] for index in range(1, len(body.allocations) + 1)
            ],
            amounts=[item.amount for item in body.allocations],
            currency=transaction["currency"],
            notes=[item.note for item in body.allocations],
        )

        transaction = await get_transaction(pool, transaction_id=transaction_id, user_id=user_id)
        allocations = await get_allocations(pool, transaction_id=transaction_id)
    except HTTPException:
        raise
    except Exception:
        logger.error(
            "Unexpected error creating allocations for transaction %s, user %s", transaction_id, user_id, exc_info=True
        )
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail=GENERIC_ERROR_MESSAGE,
        ) from None

    return {**transaction, "allocations": allocations}


class BulkAllocationMapping(BaseModel):
    transaction_id: UUID
    category_id: UUID | None = None


class BulkAllocateRequest(BaseModel):
    # Exactly one shape: uniform (transaction_ids [+ category_id]) or
    # per-transaction mapping (allocations) - validated in the handler
    # below, not here, matching this file's existing convention of
    # business-rule checks living in the route rather than the model
    # (see CreateTransactionsRequest's own sibling fields).
    transaction_ids: list[UUID] | None = None
    category_id: UUID | None = None
    allocations: list[BulkAllocationMapping] | None = None


class BulkAllocateResponse(BaseModel):
    updated: list[UUID]
    skipped: list[UUID]


@router.post("/bulk-allocate", response_model=BulkAllocateResponse, status_code=status.HTTP_201_CREATED)
async def bulk_allocate_endpoint(
    body: BulkAllocateRequest,
    user_id: UUID = Depends(get_current_user_id),
    pool: asyncpg.Pool | None = Depends(get_pool),
) -> dict:
    if pool is None:
        logger.error("Database pool unavailable")
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE, detail=GENERIC_ERROR_MESSAGE
        )

    uniform_shape = body.transaction_ids is not None
    mapping_shape = body.allocations is not None
    if uniform_shape == mapping_shape:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail="Provide exactly one of transaction_ids or allocations",
        )
    if uniform_shape and not body.transaction_ids:
        raise HTTPException(status_code=status.HTTP_422_UNPROCESSABLE_ENTITY, detail="transaction_ids can't be empty")
    if mapping_shape and not body.allocations:
        raise HTTPException(status_code=status.HTTP_422_UNPROCESSABLE_ENTITY, detail="allocations can't be empty")

    # Resolve each transaction's category_id up front, independent of shape.
    category_by_transaction: dict[UUID, UUID | None] = (
        {transaction_id: body.category_id for transaction_id in body.transaction_ids}
        if uniform_shape
        else {item.transaction_id: item.category_id for item in body.allocations}
    )

    try:
        transactions = await get_transactions_with_allocated(
            pool, transaction_ids=list(category_by_transaction.keys()), user_id=user_id
        )
        transactions_by_id = {transaction["id"]: transaction for transaction in transactions}
        missing = category_by_transaction.keys() - transactions_by_id.keys()
        if missing:
            raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Transaction not found")

        category_ids = {cid for cid in category_by_transaction.values() if cid is not None}
        categories_by_id = {}
        if category_ids:
            found = await get_categories_by_ids(pool, category_ids=list(category_ids), user_id=user_id)
            categories_by_id = {category["id"]: category for category in found}
            missing_categories = category_ids - categories_by_id.keys()
            if missing_categories:
                raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Category not found")

        # ab-60, checked for every transaction/category pair before any
        # writes happen - one mismatch rejects the whole request, same
        # all-or-nothing timing as the single-transaction endpoint.
        for transaction_id, category_id in category_by_transaction.items():
            if category_id is None:
                continue
            transaction = transactions_by_id[transaction_id]
            expected_type = _CATEGORY_TYPE_FOR_DIRECTION[transaction["direction"]]
            if categories_by_id[category_id]["type"] != expected_type:
                raise HTTPException(
                    status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
                    detail="A category's type doesn't match one of the selected transactions' direction",
                )

        # ab-59: each transaction gets exactly its own remaining balance,
        # never the full original amount - a row already partly reconciled
        # only gets the rest filled in. Nothing remaining (already fully
        # reconciled) is skipped, not errored.
        insert_transaction_ids: list[UUID] = []
        insert_category_ids: list[UUID | None] = []
        insert_amounts: list[float] = []
        insert_currencies: list[str] = []
        updated: list[UUID] = []
        skipped: list[UUID] = []

        for transaction_id, category_id in category_by_transaction.items():
            transaction = transactions_by_id[transaction_id]
            remaining = float(transaction["amount"]) - float(transaction["allocated"])
            if remaining <= 0:
                skipped.append(transaction_id)
                continue
            insert_transaction_ids.append(transaction_id)
            insert_category_ids.append(category_id)
            insert_amounts.append(remaining)
            insert_currencies.append(transaction["currency"])
            updated.append(transaction_id)

        if insert_transaction_ids:
            await insert_bulk_allocations(
                pool,
                transaction_ids=insert_transaction_ids,
                category_ids=insert_category_ids,
                amounts=insert_amounts,
                currencies=insert_currencies,
            )
    except HTTPException:
        raise
    except Exception:
        logger.error("Unexpected error bulk-allocating for user %s", user_id, exc_info=True)
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail=GENERIC_ERROR_MESSAGE,
        ) from None

    return {"updated": updated, "skipped": skipped}


class TransactionCreateItem(BaseModel):
    account_id: UUID
    sub_ledger_id: UUID | None = None
    txn_date: date
    # Validated manually against ("in", "out") below rather than typed as
    # Literal - every business-rule failure in this batch (this included)
    # reports as the same "Row N: ..." string, matching ManualEntryTable's
    # (ab-45/ab-46) own client-side messages, rather than mixing that with
    # Pydantic's structured per-field error shape for some checks only.
    direction: str
    amount: float
    description: str | None = None


class CreateTransactionsRequest(BaseModel):
    transactions: list[TransactionCreateItem] = Field(min_length=1)


class CreateTransactionsResponse(BaseModel):
    items: list[TransactionListItem]


@router.post("/batch", response_model=CreateTransactionsResponse, status_code=status.HTTP_201_CREATED)
async def create_transactions_endpoint(
    body: CreateTransactionsRequest,
    user_id: UUID = Depends(get_current_user_id),
    pool: asyncpg.Pool | None = Depends(get_pool),
) -> dict:
    if pool is None:
        logger.error("Database pool unavailable")
        raise HTTPException(status_code=status.HTTP_503_SERVICE_UNAVAILABLE, detail=GENERIC_ERROR_MESSAGE)

    try:
        accounts_by_id: dict[UUID, dict] = {}
        sub_ledgers_by_account: dict[UUID, list[dict]] = {}

        for index, item in enumerate(body.transactions, start=1):
            row_label = f"Row {index}"

            if item.account_id not in accounts_by_id:
                account = await get_account(pool, account_id=item.account_id, user_id=user_id)
                if account is None:
                    raise HTTPException(
                        status_code=status.HTTP_404_NOT_FOUND, detail=f"{row_label}: Account not found"
                    )
                accounts_by_id[item.account_id] = account
                sub_ledgers_by_account[item.account_id] = await list_sub_ledgers(
                    pool, account_id=item.account_id
                )

            sub_ledgers = sub_ledgers_by_account[item.account_id]
            if sub_ledgers:
                if item.sub_ledger_id is None:
                    raise HTTPException(
                        status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
                        detail=f"{row_label}: Sub-ledger is required",
                    )
                if not any(sl["id"] == item.sub_ledger_id for sl in sub_ledgers):
                    raise HTTPException(
                        status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
                        detail=f"{row_label}: Sub-ledger does not belong to this account",
                    )
            elif item.sub_ledger_id is not None:
                raise HTTPException(
                    status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
                    detail=f"{row_label}: This account has no sub-ledgers",
                )

            if item.txn_date > date.today():
                raise HTTPException(
                    status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
                    detail=f"{row_label}: Date cannot be in the future",
                )
            if item.direction not in ("in", "out"):
                raise HTTPException(
                    status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
                    detail=f"{row_label}: Direction must be 'in' or 'out'",
                )
            if item.amount <= 0:
                raise HTTPException(
                    status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
                    detail=f"{row_label}: Amount must be greater than zero",
                )

        created = await insert_manual_transactions(
            pool,
            account_ids=[item.account_id for item in body.transactions],
            sub_ledger_ids=[item.sub_ledger_id for item in body.transactions],
            txn_dates=[item.txn_date for item in body.transactions],
            amounts=[item.amount for item in body.transactions],
            currencies=[accounts_by_id[item.account_id]["currency"] for item in body.transactions],
            directions=[item.direction for item in body.transactions],
            descriptions=[item.description for item in body.transactions],
        )
    except HTTPException:
        raise
    except Exception:
        logger.error("Unexpected error creating manual transactions for user %s", user_id, exc_info=True)
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail=GENERIC_ERROR_MESSAGE,
        ) from None

    sub_ledger_names = {
        sub_ledger["id"]: sub_ledger["name"]
        for sub_ledgers in sub_ledgers_by_account.values()
        for sub_ledger in sub_ledgers
    }
    items = [
        {
            **row,
            "sub_ledger_name": sub_ledger_names.get(row["sub_ledger_id"]),
            # A transaction this endpoint just created can't already have
            # an allocation - always unreconciled, no need to query.
            "status": "unreconciled",
        }
        for row in created
    ]
    return {"items": items}
