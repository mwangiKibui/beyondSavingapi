"""Admin management of institutions (ab-197 create, ab-198 update, ab-199
retire/restore) over ab-184's table. The UI is ab-201; ab-294 adds the CSV
import and switches the account forms over to this list.

Institutions are never deleted - retiring keeps every link (accounts,
plan lines) valid. accounts.provider stores the name, so a rename also
renames it on accounts, and renaming a provider the app reads statements
for is refused (the reader is looked up by name).
"""

import csv
import io
import logging
import re
from datetime import datetime
from typing import Literal, get_args
from uuid import UUID

import asyncpg
from fastapi import APIRouter, Depends, File, HTTPException, Query, UploadFile, status
from pydantic import BaseModel, field_validator, model_validator

from app.core.db import get_pool
from app.core.errors import GENERIC_ERROR_MESSAGE
from app.core.security import require_admin
from app.parsers import STATEMENT_PROVIDERS
from app.repositories.institutions import (
    count_accounts_using,
    create_institution,
    existing_normalized,
    find_duplicate,
    get_institution,
    insert_institutions,
    list_institutions,
    normalize_name,
    rename_account_providers,
    set_retired,
    update_institution,
)

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/admin", tags=["admin"])

ALLOWED_PAGE_SIZES = (5, 10, 20, 30)
NAME_LIMIT = 100

InstitutionType = Literal[
    "bank", "mobile_money", "sacco", "money_market", "insurer", "pension", "government_securities",
    "digital_lender",
]
Regulator = Literal["CBK", "SASRA", "CMA", "IRA", "RBA"]
StatusFilter = Literal["active", "retired", "all"]


def _check_page_size(page_size: int) -> None:
    if page_size not in ALLOWED_PAGE_SIZES:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail=f"page_size must be one of {ALLOWED_PAGE_SIZES}",
        )


def _server_error(action: str, admin_id: UUID) -> HTTPException:
    logger.error("Unexpected error %s, admin %s", action, admin_id, exc_info=True)
    return HTTPException(status_code=status.HTTP_500_INTERNAL_SERVER_ERROR, detail=GENERIC_ERROR_MESSAGE)


def _clean_name(value: str) -> str:
    value = re.sub(r"\s+", " ", value).strip()
    if not value:
        raise ValueError("Name can't be blank")
    if len(value) > NAME_LIMIT:
        raise ValueError(f"Name must be {NAME_LIMIT} characters or fewer")
    return value


# --- models ---------------------------------------------------------------


class InstitutionOut(BaseModel):
    id: UUID
    name: str
    type: str
    regulator: str | None = None
    statements_uploadable: bool
    is_active: bool
    retired_at: datetime | None = None
    created_at: datetime
    updated_at: datetime


class InstitutionListResponse(BaseModel):
    items: list[InstitutionOut]
    total: int
    page: int
    page_size: int


class InstitutionCreate(BaseModel):
    name: str
    type: InstitutionType
    regulator: Regulator | None = None
    statements_uploadable: bool = False

    @field_validator("name")
    @classmethod
    def _name(cls, v):
        return _clean_name(v)


class InstitutionUpdate(BaseModel):
    name: str | None = None
    type: InstitutionType | None = None
    # Send null to clear it; leave it out to keep it.
    regulator: Regulator | None = None
    statements_uploadable: bool | None = None

    @field_validator("name")
    @classmethod
    def _name(cls, v):
        return v if v is None else _clean_name(v)

    @model_validator(mode="after")
    def _not_empty(self):
        if not self.model_fields_set:
            raise ValueError("Nothing to change")
        for field in ("name", "type", "statements_uploadable"):
            if field in self.model_fields_set and getattr(self, field) is None:
                raise ValueError(f"{field} can't be null")
        return self

    def changes(self) -> dict:
        return {field: getattr(self, field) for field in self.model_fields_set}


def _out(row: dict) -> dict:
    return {**row, "is_active": row.get("retired_at") is None}


def _require_reader(name: str) -> None:
    if name not in STATEMENT_PROVIDERS:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail=f"The app can't read statements from {name} yet, so uploads can't be turned on",
        )


async def _refuse_duplicate(conn, *, name: str, type: str, exclude_id: UUID | None) -> None:
    other = await find_duplicate(conn, name=name, type=type, exclude_id=exclude_id)
    if other is not None:
        state = "" if other.get("retired_at") is None else " (retired - restore it instead)"
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail=f"{other['name']} is already listed{state}",
        )


_TAKEN = HTTPException(status_code=status.HTTP_409_CONFLICT, detail="That name was just taken by another institution")


# --- list / get ---------------------------------------------------------------


@router.get("/institutions", response_model=InstitutionListResponse)
async def list_institutions_endpoint(
    page: int = Query(default=1, ge=1),
    page_size: int = Query(default=10),
    search: str | None = Query(default=None, min_length=1),
    type: InstitutionType | None = Query(default=None),
    status_filter: StatusFilter = Query(default="all", alias="status"),
    admin_id: UUID = Depends(require_admin),
    pool: asyncpg.Pool | None = Depends(get_pool),
) -> dict:
    """Every institution, retired ones included (status=active|retired|all)."""
    _check_page_size(page_size)
    try:
        async with pool.acquire() as conn:
            items, total = await list_institutions(
                conn, search=search, type=type, status=status_filter, page=page, page_size=page_size
            )
    except Exception:
        raise _server_error("listing institutions", admin_id) from None
    return {"items": [_out(i) for i in items], "total": total, "page": page, "page_size": page_size}


@router.get("/institutions/{institution_id}", response_model=InstitutionOut)
async def get_institution_endpoint(
    institution_id: UUID,
    admin_id: UUID = Depends(require_admin),
    pool: asyncpg.Pool | None = Depends(get_pool),
) -> dict:
    try:
        async with pool.acquire() as conn:
            row = await get_institution(conn, institution_id=institution_id)
    except Exception:
        raise _server_error("reading institution", admin_id) from None
    if row is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Institution not found")
    return _out(row)


# --- ab-197 create ----------------------------------------------------------


@router.post("/institutions", response_model=InstitutionOut, status_code=status.HTTP_201_CREATED)
async def create_institution_endpoint(
    body: InstitutionCreate,
    admin_id: UUID = Depends(require_admin),
    pool: asyncpg.Pool | None = Depends(get_pool),
) -> dict:
    if body.statements_uploadable:
        _require_reader(body.name)
    try:
        async with pool.acquire() as conn:
            async with conn.transaction():
                await _refuse_duplicate(conn, name=body.name, type=body.type, exclude_id=None)
                row = await create_institution(
                    conn, name=body.name, type=body.type, regulator=body.regulator,
                    statements_uploadable=body.statements_uploadable,
                )
    except HTTPException:
        raise
    except asyncpg.UniqueViolationError:
        raise _TAKEN from None
    except Exception:
        raise _server_error("creating institution", admin_id) from None
    return _out(row)


# --- ab-198 update ----------------------------------------------------------


@router.patch("/institutions/{institution_id}", response_model=InstitutionOut)
async def update_institution_endpoint(
    institution_id: UUID,
    body: InstitutionUpdate,
    admin_id: UUID = Depends(require_admin),
    pool: asyncpg.Pool | None = Depends(get_pool),
) -> dict:
    """Edits name, type, regulator and/or the uploads flag. A rename also
    renames the provider on existing accounts, in the same transaction."""
    changes = body.changes()
    try:
        async with pool.acquire() as conn:
            async with conn.transaction():
                current = await get_institution(conn, institution_id=institution_id)
                if current is None:
                    raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Institution not found")
                new_name = changes.get("name", current["name"])
                new_type = changes.get("type", current["type"])
                renamed = new_name != current["name"]
                retyped = new_type != current["type"]

                if renamed and current["name"] in STATEMENT_PROVIDERS:
                    raise HTTPException(
                        status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
                        detail=f"{current['name']} can't be renamed: the app finds its statement reader by name",
                    )
                if changes.get("statements_uploadable"):
                    _require_reader(new_name)
                if retyped:
                    in_use = await count_accounts_using(conn, name=current["name"], type=current["type"])
                    if in_use:
                        raise HTTPException(
                            status_code=status.HTTP_409_CONFLICT,
                            detail=f"{in_use} account(s) use {current['name']} as a {current['type']}; "
                                   "its type can't change while they do",
                        )
                if renamed or retyped:
                    await _refuse_duplicate(conn, name=new_name, type=new_type, exclude_id=institution_id)
                if renamed:
                    await rename_account_providers(conn, old=current["name"], new=new_name, type=current["type"])
                row = await update_institution(conn, institution_id=institution_id, changes=changes)
    except HTTPException:
        raise
    except asyncpg.UniqueViolationError:
        raise _TAKEN from None
    except Exception:
        raise _server_error("updating institution", admin_id) from None
    return _out(row)


# --- ab-199 retire / restore ------------------------------------------------


async def _set_retired(pool, *, institution_id: UUID, retired: bool, admin_id: UUID) -> dict:
    try:
        async with pool.acquire() as conn:
            async with conn.transaction():
                current = await get_institution(conn, institution_id=institution_id)
                if current is None:
                    raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Institution not found")
                if (current.get("retired_at") is not None) == retired:
                    return _out(current)
                row = await set_retired(conn, institution_id=institution_id, retired=retired)
    except HTTPException:
        raise
    except Exception:
        raise _server_error("retiring institution" if retired else "restoring institution", admin_id) from None
    return _out(row)


@router.post("/institutions/{institution_id}/retire", response_model=InstitutionOut)
async def retire_institution_endpoint(
    institution_id: UUID,
    admin_id: UUID = Depends(require_admin),
    pool: asyncpg.Pool | None = Depends(get_pool),
) -> dict:
    """Hides it from pickers; nothing is deleted. Retiring twice is fine."""
    return await _set_retired(pool, institution_id=institution_id, retired=True, admin_id=admin_id)


@router.post("/institutions/{institution_id}/restore", response_model=InstitutionOut)
async def restore_institution_endpoint(
    institution_id: UUID,
    admin_id: UUID = Depends(require_admin),
    pool: asyncpg.Pool | None = Depends(get_pool),
) -> dict:
    return await _set_retired(pool, institution_id=institution_id, retired=False, admin_id=admin_id)


# --- ab-294 CSV import --------------------------------------------------------
# Preview then confirm. Stateless: confirm re-reads the same file, so what
# goes in is whatever the file says at that moment. Only the four account
# types are accepted, and imported rows never have uploads turned on - an
# admin does that per institution once the app has a reader.

IMPORT_TYPES = ("bank", "mobile_money", "sacco", "money_market")
IMPORT_HEADERS = ("name", "account_type", "regulator")
IMPORT_MAX_ROWS = 1000
IMPORT_MAX_BYTES = 200 * 1024
REGULATORS = get_args(Regulator)

ImportStatus = Literal["add", "exists", "invalid"]


class ImportRow(BaseModel):
    row: int
    name: str
    account_type: str
    regulator: str | None = None
    status: ImportStatus
    reason: str | None = None


class ImportPreview(BaseModel):
    rows: list[ImportRow]
    add: int
    exists: int
    invalid: int


class ImportResult(BaseModel):
    added: int
    exists: int
    invalid: int


def _unprocessable(detail: str) -> HTTPException:
    return HTTPException(status_code=status.HTTP_422_UNPROCESSABLE_ENTITY, detail=detail)


def _parse_import(filename: str, content: bytes) -> list[dict]:
    """The file's rows, each checked on its own and against the earlier
    rows (status add / invalid; the table check comes after). Raises 422
    when the file as a whole can't be used."""
    if not filename.lower().endswith(".csv"):
        raise _unprocessable("Choose a .csv file")
    if len(content) > IMPORT_MAX_BYTES:
        raise _unprocessable("The file must be 200KB or smaller")
    try:
        text = content.decode("utf-8-sig")
    except UnicodeDecodeError:
        raise _unprocessable("Save the file as CSV UTF-8") from None

    reader = csv.reader(io.StringIO(text))
    header = next(reader, None) or []
    # "Account Type" or "account-type" from a spreadsheet = account_type.
    columns = {re.sub(r"[\s-]+", "_", h.strip().lower()): i for i, h in enumerate(header)}
    if any(h not in columns for h in IMPORT_HEADERS):
        raise _unprocessable("The file needs name, account_type and regulator columns")

    def cell(values: list[str], key: str) -> str:
        i = columns[key]
        return values[i].strip() if i < len(values) else ""

    rows, seen = [], set()
    for line_no, values in enumerate(reader, start=2):
        if not any(v.strip() for v in values):
            continue
        if len(rows) == IMPORT_MAX_ROWS:
            raise _unprocessable(f"The file can have at most {IMPORT_MAX_ROWS} rows")
        raw_name = cell(values, "name")
        kind = re.sub(r"[\s-]+", "_", cell(values, "account_type").lower())
        regulator = cell(values, "regulator").upper() or None
        row = {"row": line_no, "name": re.sub(r"\s+", " ", raw_name).strip(), "account_type": kind,
               "regulator": regulator, "status": "add", "reason": None}
        try:
            row["name"] = _clean_name(raw_name)
            if kind not in IMPORT_TYPES:
                raise ValueError(f"Type must be one of {', '.join(IMPORT_TYPES)}")
            if regulator is not None and regulator not in REGULATORS:
                raise ValueError(f"Regulator must be blank or one of {', '.join(REGULATORS)}")
            key = (normalize_name(row["name"]), kind)
            if key in seen:
                raise ValueError("Repeated in this file")
            seen.add(key)
        except ValueError as exc:
            row["status"], row["reason"] = "invalid", str(exc)
        rows.append(row)
    return rows


async def _mark_existing(conn, rows: list[dict]) -> None:
    candidates = [r for r in rows if r["status"] == "add"]
    keys = {(normalize_name(r["name"]), r["account_type"]) for r in candidates}
    existing = await existing_normalized(conn, keys=keys)
    for r in candidates:
        retired = existing.get((normalize_name(r["name"]), r["account_type"]))
        if retired is not None:
            r["status"] = "exists"
            r["reason"] = "Already there (retired - restore it)" if retired else "Already there"


def _counts(rows: list[dict]) -> dict:
    return {s: sum(r["status"] == s for r in rows) for s in ("add", "exists", "invalid")}


async def _read_upload(file: UploadFile) -> list[dict]:
    content = await file.read(IMPORT_MAX_BYTES + 1)
    return _parse_import(file.filename or "", content)


@router.post("/institutions/import/preview", response_model=ImportPreview)
async def preview_institution_import_endpoint(
    file: UploadFile = File(...),
    admin_id: UUID = Depends(require_admin),
    pool: asyncpg.Pool | None = Depends(get_pool),
) -> dict:
    """What importing this CSV would do, row by row. Changes nothing."""
    rows = await _read_upload(file)
    try:
        async with pool.acquire() as conn:
            await _mark_existing(conn, rows)
    except Exception:
        raise _server_error("previewing institution import", admin_id) from None
    return {"rows": rows, **_counts(rows)}


@router.post("/institutions/import", response_model=ImportResult)
async def import_institutions_endpoint(
    file: UploadFile = File(...),
    admin_id: UUID = Depends(require_admin),
    pool: asyncpg.Pool | None = Depends(get_pool),
) -> dict:
    """Adds the file's new rows in one transaction (uploads off). Rows
    already listed, retired ones included, are skipped, so importing the
    same file twice adds nothing the second time."""
    rows = await _read_upload(file)
    try:
        async with pool.acquire() as conn:
            async with conn.transaction():
                await _mark_existing(conn, rows)
                new = [{"name": r["name"], "type": r["account_type"], "regulator": r["regulator"]}
                       for r in rows if r["status"] == "add"]
                added = await insert_institutions(conn, rows=new) if new else 0
    except Exception:
        raise _server_error("importing institutions", admin_id) from None
    counts = _counts(rows)
    # Anything that lost a race to another admin counts as already there.
    return {"added": added, "exists": counts["exists"] + counts["add"] - added, "invalid": counts["invalid"]}
