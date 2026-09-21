import logging
from datetime import date, datetime
from typing import Literal
from uuid import UUID, uuid4

import asyncpg
from fastapi import APIRouter, Depends, File, Form, HTTPException, Query, UploadFile, status
from pydantic import BaseModel

from app.core.config import get_settings
from app.core.db import get_pool
from app.core.queues import PARSE_JOBS_QUEUE
from app.core.redis import get_redis
from app.core.security import get_current_user_id
from app.core.storage import get_storage_client
from app.repositories.accounts import get_account
from app.repositories.statement_imports import (
    add_import_sub_ledgers,
    create_statement_import,
    list_statement_imports,
)
from app.repositories.sub_ledgers import list_sub_ledgers
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


class StatementImportListResponse(BaseModel):
    items: list[StatementImportListItem]
    total: int
    page: int
    page_size: int


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
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE, detail="Database unavailable"
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
            detail="Something went wrong. Please try again.",
        ) from None

    return {"items": items, "total": total, "page": page, "page_size": page_size}


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
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE, detail="Database unavailable"
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
            Bucket=settings.minio_bucket, Key=storage_key, Body=content
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
            detail="Something went wrong. Please try again.",
        ) from None

    return statement_import
