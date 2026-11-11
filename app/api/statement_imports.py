import logging
import re
from datetime import date, datetime
from typing import Literal
from uuid import UUID, uuid4

import asyncpg
from fastapi import APIRouter, Depends, File, Form, HTTPException, Query, Response, UploadFile, status
from pydantic import BaseModel, Field, field_validator

from app.core.config import get_settings
from app.core.db import get_pool
from app.core.errors import GENERIC_ERROR_MESSAGE
from app.core.queues import PARSE_JOBS_QUEUE
from app.core.redis import get_redis
from app.core.security import get_current_user_id
from app.core.storage import get_storage_client
from app.repositories.accounts import get_account
from app.repositories.category_suggestions import (
    get_approved_directory,
    get_blocked_numbers,
    get_import_for_preview,
    get_import_rows,
    get_learned_names,
    get_shared_votes,
    get_user_categories,
    get_user_rules,
)
from app.repositories.import_progress import delete_progress, get_progress, upsert_progress
from app.repositories.statement_imports import (
    add_import_sub_ledgers,
    create_statement_import,
    list_statement_imports,
)
from app.repositories.notifications import generate_budget_alerts
from app.repositories.sub_ledgers import list_sub_ledgers
from app.services.allocations import AllocationRejected, alert_groups
from app.services.category_suggestions import (
    Directory,
    apply_learned_names,
    build_preview_rows,
    extract_keys,
    learned_name_lookups,
    new_category_names,
    rule_lookup_pairs,
    shared_vote_lookups,
)
from app.services.import_confirm import ConfirmItem, confirm_import
from app.services.import_progress import Draft, payee_keys, progress_counts
from app.services.preview_csv import build_preview_csv
from app.services.statement_files import (
    IncorrectStatementPassword,
    check_password,
    decrypt_if_needed,
)

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/statement-imports", tags=["statement-imports"])

ALLOWED_EXTENSIONS = (".pdf", ".doc", ".docx")
MAX_FILE_SIZE_BYTES = 10 * 1024 * 1024  # 10MB

ALLOWED_PAGE_SIZES = (5, 10, 20, 30)
ImportStatus = Literal["pending", "parsed", "failed"]
SortDir = Literal["asc", "desc"]


class StatementImportResponse(BaseModel):
    id: UUID
    account_id: UUID
    file_name: str
    status: str
    period_start: date | None = None
    period_end: date | None = None
    row_count: int | None = None
    error_detail: str | None = None
    created_at: datetime


class ListProgress(BaseModel):
    step: str
    reviewed_count: int
    total_count: int
    updated_at: datetime


class StatementImportListItem(BaseModel):
    id: UUID
    account_id: UUID
    account_nickname: str
    file_name: str
    status: str
    period_start: date | None = None
    period_end: date | None = None
    row_count: int | None = None
    error_detail: str | None = None
    created_at: datetime
    # ab-265: set once the review was confirmed ("Done").
    reviewed_at: datetime | None = None
    # ab-265: an unfinished review's saved progress; null when none.
    progress: ListProgress | None = None


def _list_item(row: dict) -> dict:
    step = row.pop("progress_step", None)
    reviewed = row.pop("progress_reviewed_count", None)
    total = row.pop("progress_total_count", None)
    updated = row.pop("progress_updated_at", None)
    if step is not None:
        row["progress"] = {"step": step, "reviewed_count": reviewed, "total_count": total, "updated_at": updated}
    return row


class StatementImportListResponse(BaseModel):
    items: list[StatementImportListItem]
    total: int
    page: int
    page_size: int


class PreviewImport(BaseModel):
    id: UUID
    account_id: UUID
    account_nickname: str
    provider: str
    file_name: str
    status: str
    period_start: date | None = None
    period_end: date | None = None
    row_count: int | None = None
    created_at: datetime
    reviewed_at: datetime | None = None


class PreviewSuggestion(BaseModel):
    # None when is_new_category: the category is created on confirm.
    category_id: UUID | None = None
    category_name: str
    is_new_category: bool
    # others = what most users chose (3+ voted, a clear leader); overrides
    # directory (the starter/admin list).
    source: Literal["your_rule", "this_statement", "others", "directory"]
    merchant_name: str | None = None


class PreviewRow(BaseModel):
    transaction_id: UUID
    txn_date: date
    amount: float
    currency: str
    direction: str
    description: str | None = None
    display_name: str
    label: str | None = None
    # Same value = same payee; the UI offers one choice for the whole group.
    group_key: str | None = None
    key_kind: str | None = None
    number_kind: str | None = None
    number: str | None = None
    is_business: bool
    is_transfer_hint: bool
    # display_name was learned from other users' M-Pesa statements.
    name_learned: bool = False
    # ab-260: set on rows that never get a category suggestion, so the UI
    # can label them (borrowed money is not income; Fuliza is counted via
    # the purchase it covered; SACCO loan-ledger rows are counted once on
    # the savings ledger).
    row_kind: Literal["loan_payout", "loan_request", "overdraft_draw", "overdraft_repayment", "loan_ledger"] | None = None
    # Fuliza covered (part of) this purchase. Informational only.
    paid_with_overdraft: bool = False
    suggestion: PreviewSuggestion | None = None


class PreviewSummary(BaseModel):
    open_rows: int
    suggested: int
    new_categories: list[str]


class StatementImportPreviewResponse(BaseModel):
    # "import" is a Python keyword, hence the alias.
    import_: PreviewImport = Field(alias="import", serialization_alias="import")
    rows: list[PreviewRow]
    summary: PreviewSummary


@router.get("", response_model=StatementImportListResponse)
async def list_statement_imports_endpoint(
    user_id: UUID = Depends(get_current_user_id),
    pool: asyncpg.Pool | None = Depends(get_pool),
    page: int = Query(default=1, ge=1),
    page_size: int = Query(default=10),
    search: str | None = Query(default=None, min_length=1),
    status_filter: ImportStatus | None = Query(default=None, alias="status"),
    account_id: UUID | None = Query(default=None),
    sort_dir: SortDir = "desc",
) -> dict:
    # Literal[5, 10, 20, 30] doesn't reliably coerce a query string ("20")
    # against int literals in Pydantic v2, so this is validated manually
    # rather than via the type annotation - matches this repo's other
    # paginated list endpoints (ab-24).
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
        items, total = await list_statement_imports(
            pool,
            user_id=user_id,
            page=page,
            page_size=page_size,
            search=search,
            status=status_filter,
            account_id=account_id,
            sort_dir=sort_dir,
        )
    except Exception:
        logger.error("Unexpected error listing statement imports for user %s", user_id, exc_info=True)
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail=GENERIC_ERROR_MESSAGE,
        ) from None

    return {"items": [_list_item(dict(item)) for item in items], "total": total, "page": page, "page_size": page_size}


@router.post("", response_model=StatementImportResponse, status_code=status.HTTP_201_CREATED)
async def upload_statement_endpoint(
    account_id: UUID = Form(...),
    password: str | None = Form(default=None),
    # Which of the account's sub-ledgers to import from this file (ab-119) -
    # must be empty for an account with none, and non-empty (naming only
    # this account's own sub-ledgers) for one that has them. Repeated
    # multipart fields, e.g. sub_ledger_ids=<id1>&sub_ledger_ids=<id2>.
    sub_ledger_ids: list[UUID] = Form(default=[]),
    file: UploadFile = File(...),
    user_id: UUID = Depends(get_current_user_id),
    pool: asyncpg.Pool | None = Depends(get_pool),
) -> dict:
    if pool is None:
        logger.error("Database pool unavailable")
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE, detail=GENERIC_ERROR_MESSAGE
        )

    file_name = file.filename or ""
    if not file_name.lower().endswith(ALLOWED_EXTENSIONS):
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail="File must be a PDF or Word document (.pdf, .doc, .docx)",
        )

    content = await file.read()
    if len(content) > MAX_FILE_SIZE_BYTES:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail="File must be 10MB or smaller",
        )

    try:
        account = await get_account(pool, account_id=account_id, user_id=user_id)
        if account is None:
            raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Account not found")

        account_sub_ledgers = await list_sub_ledgers(pool, account_id=account_id)
        if account_sub_ledgers:
            valid_ids = {sub_ledger["id"] for sub_ledger in account_sub_ledgers}
            if not sub_ledger_ids:
                raise HTTPException(
                    status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
                    detail="Select at least one sub-ledger to import from this statement",
                )
            if not set(sub_ledger_ids).issubset(valid_ids):
                raise HTTPException(
                    status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
                    detail="One or more selected sub-ledgers don't belong to this account",
                )
        elif sub_ledger_ids:
            raise HTTPException(
                status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
                detail="This account has no sub-ledgers",
            )

        # Own try/except, distinct from the whole-operation catch-all below -
        # a wrong/missing password is an expected rejection that must return
        # before any row/file/queue side effects, not get swallowed by (or
        # confused with) an unrelated unexpected error.
        try:
            check_password(content=content, filename=file_name, password=password)
        except IncorrectStatementPassword as exc:
            raise HTTPException(
                status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
                detail="Incorrect statement password",
            ) from exc

        # Strips PDF encryption before storing - the password itself is
        # never persisted (see check_password's docstring), but the file
        # still needs to be readable later without it (ab-39's parser).
        content = decrypt_if_needed(content=content, filename=file_name, password=password)

        import_id = uuid4()
        storage_key = f"statements/{import_id}/{file_name}"

        settings = get_settings()
        get_storage_client().put_object(
            Bucket=settings.storage_bucket, Key=storage_key, Body=content
        )

        statement_import = await create_statement_import(
            pool,
            import_id=import_id,
            account_id=account_id,
            file_name=file_name,
            storage_key=storage_key,
        )

        # Persisted before enqueueing - the worker picks the job up in a
        # separate process and needs a durable record of which
        # sub-ledgers were selected, not just the transient job message.
        await add_import_sub_ledgers(pool, import_id=import_id, sub_ledger_ids=sub_ledger_ids)

        await get_redis().lpush(PARSE_JOBS_QUEUE, str(import_id))
    except HTTPException:
        raise
    except Exception:
        logger.error("Unexpected error uploading statement for user %s", user_id, exc_info=True)
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail=GENERIC_ERROR_MESSAGE,
        ) from None

    return statement_import


async def _load_preview(pool: asyncpg.Pool, *, import_id: UUID, user_id: UUID) -> dict:
    """Everything the preview and its CSV are built from. Raises the 404 /
    409 / 500 HTTPExceptions both endpoints share."""
    try:
        statement_import = await get_import_for_preview(pool, import_id=import_id, user_id=user_id)
        if statement_import is None:
            raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Statement import not found")
        if statement_import["status"] != "parsed":
            raise HTTPException(
                status_code=status.HTTP_409_CONFLICT,
                detail="This statement hasn't finished processing yet",
            )

        rows = await get_import_rows(
            pool,
            import_id=import_id,
            account_id=statement_import["account_id"],
            period_start=statement_import["period_start"],
            period_end=statement_import["period_end"],
        )
        keys = extract_keys(
            rows, provider=statement_import["provider"], secret=get_settings().suggestion_key_secret
        )
        directory = Directory.from_rows(
            await get_approved_directory(pool), blocked=await get_blocked_numbers(pool)
        )
        # Rows showing only a Till/Paybill borrow the name other users' M-Pesa
        # statements printed for it (ab-253 PR 4).
        learned = await get_learned_names(pool, numbers=learned_name_lookups(rows, keys, directory))
        keys = apply_learned_names(rows, keys, learned, directory)
        rules = await get_user_rules(pool, user_id=user_id, pairs=rule_lookup_pairs(keys.values(), directory))
        categories = await get_user_categories(pool, user_id=user_id)
        name_keys, number_keys = shared_vote_lookups(rows, keys, directory)
        shared_votes = await get_shared_votes(pool, name_keys=name_keys, number_keys=number_keys)
    except HTTPException:
        raise
    except Exception:
        logger.error(
            "Unexpected error building preview for import %s, user %s", import_id, user_id, exc_info=True
        )
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail=GENERIC_ERROR_MESSAGE,
        ) from None

    preview_rows = build_preview_rows(
        rows, keys=keys, rules=rules, directory=directory, categories=categories, shared_votes=shared_votes
    )
    return {
        "import": statement_import,
        "rows": rows,
        "keys": keys,
        "categories": categories,
        "preview_rows": preview_rows,
    }


def _require_pool(pool: asyncpg.Pool | None) -> None:
    if pool is None:
        logger.error("Database pool unavailable")
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE, detail=GENERIC_ERROR_MESSAGE
        )


@router.get(
    "/{import_id}/preview",
    response_model=StatementImportPreviewResponse,
    response_model_by_alias=True,
)
async def preview_statement_import_endpoint(
    import_id: UUID,
    user_id: UUID = Depends(get_current_user_id),
    pool: asyncpg.Pool | None = Depends(get_pool),
) -> dict:
    """The import's open rows (nothing allocated yet, not a transfer) with
    a suggested category each where one is known (ab-253). Read-only:
    categories are only saved by the confirm step."""
    _require_pool(pool)
    loaded = await _load_preview(pool, import_id=import_id, user_id=user_id)
    preview_rows = loaded["preview_rows"]
    return {
        "import": loaded["import"],
        "rows": preview_rows,
        "summary": {
            "open_rows": len(preview_rows),
            "suggested": sum(1 for r in preview_rows if r["suggestion"]),
            "new_categories": new_category_names(preview_rows),
        },
    }


@router.get("/{import_id}/preview.csv", response_class=Response)
async def preview_statement_import_csv_endpoint(
    import_id: UUID,
    user_id: UUID = Depends(get_current_user_id),
    pool: asyncpg.Pool | None = Depends(get_pool),
) -> Response:
    """The preview as a CSV download (ab-253): every row the statement
    covers, with its suggestion while open and its chosen category once
    categorised, to compare with the user's own records."""
    _require_pool(pool)
    loaded = await _load_preview(pool, import_id=import_id, user_id=user_id)
    content = build_preview_csv(
        loaded["rows"],
        preview_rows=loaded["preview_rows"],
        categories=loaded["categories"],
        keys=loaded["keys"],
    )
    stem = re.sub(r"[^A-Za-z0-9._-]+", "-", loaded["import"]["file_name"].rsplit(".", 1)[0]).strip("-") or "statement"
    return Response(
        content=content,
        media_type="text/csv; charset=utf-8",
        headers={"Content-Disposition": f'attachment; filename="{stem}-suggestions.csv"'},
    )


def _blank_to_none(value: str | None) -> str | None:
    if value is None:
        return None
    value = value.strip()
    return value or None


class ConfirmItemRequest(BaseModel):
    transaction_id: UUID
    # Exactly one of category_id / new_category_name (checked in the
    # handler, like bulk-allocate's shape check).
    category_id: UUID | None = None
    new_category_name: str | None = None
    # The user's private "Who is this?" for the payee.
    label: str | None = None
    # Only for rows showing an account reference (NCBA PayBill rows).
    user_entered_paybill: str | None = Field(default=None, pattern=r"^[0-9]{5,8}$")

    @field_validator("new_category_name", "label", mode="before")
    @classmethod
    def _strip(cls, value):
        return _blank_to_none(value) if isinstance(value, str) else value

    @field_validator("new_category_name")
    @classmethod
    def _name_length(cls, value):
        if value is not None and len(value) > 60:
            raise ValueError("Category name must be 60 characters or fewer")
        return value

    @field_validator("label")
    @classmethod
    def _label_length(cls, value):
        if value is not None and len(value) > 40:
            raise ValueError("Label must be 40 characters or fewer")
        return value


class ConfirmRequest(BaseModel):
    items: list[ConfirmItemRequest] = Field(min_length=1, max_length=5000)


class CreatedCategory(BaseModel):
    id: UUID
    name: str
    type: str


class ConfirmResponse(BaseModel):
    allocated: list[UUID]
    # Rows already fully allocated (e.g. confirmed in an earlier sitting).
    skipped: list[UUID]
    created_categories: list[CreatedCategory]
    # Rows that taught the user's rules.
    learned: int
    reviewed_at: datetime | None = None


@router.post("/{import_id}/confirm", response_model=ConfirmResponse)
async def confirm_statement_import_endpoint(
    import_id: UUID,
    body: ConfirmRequest,
    user_id: UUID = Depends(get_current_user_id),
    pool: asyncpg.Pool | None = Depends(get_pool),
) -> dict:
    """Saves the categories chosen in the preview (ab-253): creates any new
    categories, allocates the rows, learns the user's rules and shared-list
    votes, and marks the import reviewed - all in one transaction. Rows
    left out stay unreconciled; confirming again later is fine."""
    if pool is None:
        logger.error("Database pool unavailable")
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE, detail=GENERIC_ERROR_MESSAGE
        )

    seen: set[UUID] = set()
    for entry in body.items:
        if (entry.category_id is None) == (entry.new_category_name is None):
            raise HTTPException(
                status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
                detail="Each row needs exactly one of category_id or new_category_name",
            )
        if entry.transaction_id in seen:
            raise HTTPException(
                status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
                detail="A row appears more than once",
            )
        seen.add(entry.transaction_id)

    try:
        statement_import = await get_import_for_preview(pool, import_id=import_id, user_id=user_id)
        if statement_import is None:
            raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Statement import not found")
        if statement_import["status"] != "parsed":
            raise HTTPException(
                status_code=status.HTTP_409_CONFLICT,
                detail="This statement hasn't finished processing yet",
            )

        async with pool.acquire() as conn:
            async with conn.transaction():
                result = await confirm_import(
                    conn,
                    user_id=user_id,
                    statement_import=statement_import,
                    items=[ConfirmItem(**entry.model_dump()) for entry in body.items],
                    secret=get_settings().suggestion_key_secret,
                )
    except AllocationRejected as exc:
        raise HTTPException(status_code=exc.status_code, detail=exc.detail) from None
    except HTTPException:
        raise
    except Exception:
        logger.error("Unexpected error confirming import %s, user %s", import_id, user_id, exc_info=True)
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail=GENERIC_ERROR_MESSAGE,
        ) from None

    # After commit, same as bulk-allocate (ab-77): a failure here is logged,
    # never undoes the saved categories.
    try:
        for (category_id, txn_date, currency), amount in alert_groups(
            result.plan, result.transactions_by_id
        ).items():
            await generate_budget_alerts(
                pool,
                user_id=user_id,
                category_id=category_id,
                txn_date=txn_date,
                currency=currency,
                new_amount=amount,
            )
    except Exception:
        logger.error("Unexpected error generating budget alerts after confirm, user %s", user_id, exc_info=True)

    return {
        "allocated": result.allocated,
        "skipped": result.skipped,
        "created_categories": result.created_categories,
        "learned": result.learned,
        "reviewed_at": result.reviewed_at,
    }


# --- saved progress (ab-265) ------------------------------------------------
# The upload wizard (ab-266) saves the review as the user goes so it can be
# resumed on any device; the uploads list (ab-267) shows "N of M". The row
# is deleted on confirm (reviewed_at records "finished") or on discard.

ProgressStep = Literal["processing", "review", "failed", "done"]


class ProgressResponse(BaseModel):
    step: ProgressStep
    draft: Draft
    reviewed_count: int
    total_count: int
    updated_at: datetime | None = None
    # Why the parse failed, for the wizard's Processing step (ab-266).
    error_detail: str | None = None


class SaveProgressRequest(BaseModel):
    # Only the review step holds anything to save; processing/failed/done
    # come from the import itself. ab-268 adds "transfers".
    step: Literal["review"]
    draft: Draft


def _derived_step(statement_import: dict) -> str:
    if statement_import["status"] == "pending":
        return "processing"
    if statement_import["status"] == "failed":
        return "failed"
    return "done" if statement_import.get("reviewed_at") else "review"


async def _owned_import(pool: asyncpg.Pool, *, import_id: UUID, user_id: UUID) -> dict:
    statement_import = await get_import_for_preview(pool, import_id=import_id, user_id=user_id)
    if statement_import is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Statement import not found")
    return statement_import


@router.get("/{import_id}/progress", response_model=ProgressResponse)
async def get_import_progress_endpoint(
    import_id: UUID,
    user_id: UUID = Depends(get_current_user_id),
    pool: asyncpg.Pool | None = Depends(get_pool),
) -> dict:
    _require_pool(pool)
    try:
        statement_import = await _owned_import(pool, import_id=import_id, user_id=user_id)
        saved = await get_progress(pool, import_id=import_id)
    except HTTPException:
        raise
    except Exception:
        logger.error("Unexpected error loading progress for import %s, user %s", import_id, user_id, exc_info=True)
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR, detail=GENERIC_ERROR_MESSAGE
        ) from None
    if saved is not None:
        return {**saved, "error_detail": None}
    return {
        "step": _derived_step(statement_import),
        "draft": Draft(),
        "reviewed_count": 0,
        "total_count": 0,
        "error_detail": statement_import.get("error_detail") if statement_import["status"] == "failed" else None,
    }


@router.put("/{import_id}/progress", response_model=ProgressResponse)
async def save_import_progress_endpoint(
    import_id: UUID,
    body: SaveProgressRequest,
    user_id: UUID = Depends(get_current_user_id),
    pool: asyncpg.Pool | None = Depends(get_pool),
) -> dict:
    """Replaces the whole saved draft. Last save wins."""
    _require_pool(pool)
    try:
        statement_import = await _owned_import(pool, import_id=import_id, user_id=user_id)
        if statement_import["status"] != "parsed":
            raise HTTPException(
                status_code=status.HTTP_409_CONFLICT, detail="This statement hasn't finished processing yet"
            )
        rows = await get_import_rows(
            pool,
            import_id=import_id,
            account_id=statement_import["account_id"],
            period_start=statement_import["period_start"],
            period_end=statement_import["period_end"],
        )
        keys = extract_keys(rows, provider=statement_import["provider"], secret=get_settings().suggestion_key_secret)
        draft = body.draft
        row_ids = {row["id"] for row in rows}
        if not set(draft.rows) <= row_ids:
            raise HTTPException(
                status_code=status.HTTP_422_UNPROCESSABLE_ENTITY, detail="A saved row isn't part of this statement"
            )
        payees = payee_keys(rows, keys)
        if not (set(draft.labels) | set(draft.paybills)) <= payees:
            raise HTTPException(
                status_code=status.HTTP_422_UNPROCESSABLE_ENTITY, detail="A saved payee isn't part of this statement"
            )
        picked = {entry.category_id for entry in draft.rows.values() if entry.category_id is not None}
        if picked:
            own = {category["id"] for category in await get_user_categories(pool, user_id=user_id)}
            if not picked <= own:
                raise HTTPException(status_code=status.HTTP_422_UNPROCESSABLE_ENTITY, detail="Unknown category")
        reviewed_count, total_count = progress_counts(rows, keys, draft.model_dump())
        return await upsert_progress(
            pool,
            import_id=import_id,
            step=body.step,
            draft=draft.model_dump(mode="json"),
            reviewed_count=reviewed_count,
            total_count=total_count,
        )
    except HTTPException:
        raise
    except Exception:
        logger.error("Unexpected error saving progress for import %s, user %s", import_id, user_id, exc_info=True)
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR, detail=GENERIC_ERROR_MESSAGE
        ) from None


@router.delete("/{import_id}/progress", status_code=status.HTTP_204_NO_CONTENT)
async def discard_import_progress_endpoint(
    import_id: UUID,
    user_id: UUID = Depends(get_current_user_id),
    pool: asyncpg.Pool | None = Depends(get_pool),
) -> Response:
    """Discard: drops the saved picks only. The transactions stay and can
    still be categorised later."""
    _require_pool(pool)
    try:
        await _owned_import(pool, import_id=import_id, user_id=user_id)
        await delete_progress(pool, import_id=import_id)
    except HTTPException:
        raise
    except Exception:
        logger.error("Unexpected error discarding progress for import %s, user %s", import_id, user_id, exc_info=True)
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR, detail=GENERIC_ERROR_MESSAGE
        ) from None
    return Response(status_code=status.HTTP_204_NO_CONTENT)
