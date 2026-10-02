import csv
import io
import logging
from datetime import date
from decimal import Decimal
from typing import Literal
from uuid import UUID

import asyncpg
from fastapi import APIRouter, Depends, HTTPException, Query, Response, status
from pydantic import BaseModel

from app.core.db import get_pool
from app.core.errors import GENERIC_ERROR_MESSAGE
from app.core.security import get_current_user_id
from app.repositories.accounts import get_account
from app.repositories.reports import (
    ReportFilters,
    build_report,
    fetch_report_rows,
    get_account_summary,
    get_budget_plans_report,
    get_category_summary,
    get_report,
    get_report_csv_rows,
    get_transfers,
    resolve_report_currency,
)
from app.services.report_pdf import build_report_pdf, build_simple_table_pdf

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/reports", tags=["reports"])

# Matches transactions.py's list endpoint convention: the ticket wording
# calls this filter "type", but this codebase's established name
# everywhere else is "direction" - kept consistent here rather than
# introducing a second name for the same thing.
Direction = Literal["in", "out"]

# Category Summary export (ab-152) is shared by the Expenses and Income
# report views - categories.type's own column values, reused here rather
# than inventing a second "expense"/"income" vocabulary.
CategoryType = Literal["expense", "income"]


async def _build_filters(
    pool: asyncpg.Pool,
    *,
    user_id: UUID,
    account_id: UUID | None,
    from_date: date | None,
    to_date: date | None,
    direction: str | None,
    currency: str | None,
) -> tuple[ReportFilters, dict | None]:
    """Shared by all three /reports endpoints (ab-82/83/84/85/127) so the
    exact same filter set backs the JSON report, the CSV export, and the
    PDF export - a 404 on a bad account_id and the default_currency
    fallback only ever need writing once. Returns the resolved account
    row too (None if account_id wasn't given), for the PDF export's own
    filter-summary line.
    """
    account = None
    if account_id is not None:
        account = await get_account(pool, account_id=account_id, user_id=user_id)
        if account is None:
            raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Account not found")

    resolved_currency = await resolve_report_currency(pool, user_id=user_id, currency=currency)
    filters = ReportFilters(
        user_id=user_id,
        currency=resolved_currency,
        account_id=account_id,
        from_date=from_date,
        to_date=to_date,
        direction=direction,
    )
    return filters, account


class CategoryBreakdownItem(BaseModel):
    category_id: UUID
    category_name: str | None
    category_type: str | None
    total: Decimal


class ReportResponse(BaseModel):
    currency: str
    total_in: Decimal
    total_out: Decimal
    net: Decimal
    by_category: list[CategoryBreakdownItem]
    reconciled_no_category_total: Decimal
    unreconciled_total: Decimal


class AccountSummaryItem(BaseModel):
    account_id: UUID
    account_nickname: str
    money_in: Decimal
    money_out: Decimal


class AccountSummaryResponse(BaseModel):
    items: list[AccountSummaryItem]


class TransferItem(BaseModel):
    id: UUID
    transfer_reason_name: str | None
    source_label: str
    destination_account_name: str
    amount: Decimal
    currency: str
    date: date


class TransferListResponse(BaseModel):
    items: list[TransferItem]


def _require_pool(pool: asyncpg.Pool | None) -> None:
    if pool is None:
        logger.error("Database pool unavailable")
        raise HTTPException(status_code=status.HTTP_503_SERVICE_UNAVAILABLE, detail=GENERIC_ERROR_MESSAGE)


def _csv_response(column_headers: list[str], rows: list[list], *, filename: str) -> Response:
    """Shared CSV-writing boilerplate for every ab-152 export - same
    csv.writer/io.StringIO pattern as export_report_csv_endpoint (ab-84)
    below, just parameterized by headers/rows/filename so the four new
    export types don't each reimplement it.
    """
    buffer = io.StringIO()
    writer = csv.writer(buffer)
    writer.writerow(column_headers)
    for row in rows:
        writer.writerow(row)
    return Response(
        content=buffer.getvalue(),
        media_type="text/csv",
        headers={"Content-Disposition": f'attachment; filename="{filename}"'},
    )


def _pdf_response(pdf_bytes: bytes, *, filename: str) -> Response:
    """Shared PDF Response-wrapping boilerplate, mirroring
    export_report_pdf_endpoint's own Content-Disposition/media_type
    convention (ab-85) for the four new ab-152 export types."""
    return Response(
        content=pdf_bytes,
        media_type="application/pdf",
        headers={"Content-Disposition": f'attachment; filename="{filename}"'},
    )


def _format_budget_plan_window(starts_at, ends_at) -> str:
    """Formats a budget_plans row's TIMESTAMPTZ starts_at/ends_at
    (docs/schema.sql) down to a single "start - end" calendar-date
    string for the Budget Plans export's one combined window column -
    the export is a calendar-date-level report, same as every other
    /reports export, so the stored time-of-day isn't shown."""
    return f"{starts_at.date().isoformat()} - {ends_at.date().isoformat()}"


@router.get("", response_model=ReportResponse)
async def get_report_endpoint(
    user_id: UUID = Depends(get_current_user_id),
    pool: asyncpg.Pool | None = Depends(get_pool),
    account_id: UUID | None = Query(default=None),
    from_date: date | None = Query(default=None, alias="from"),
    to_date: date | None = Query(default=None, alias="to"),
    direction: Direction | None = None,
    currency: str | None = Query(default=None),
) -> dict:
    _require_pool(pool)

    try:
        filters, _account = await _build_filters(
            pool,
            user_id=user_id,
            account_id=account_id,
            from_date=from_date,
            to_date=to_date,
            direction=direction,
            currency=currency,
        )
        report = await get_report(pool, filters)
    except HTTPException:
        raise
    except Exception:
        logger.error("Unexpected error building report for user %s", user_id, exc_info=True)
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail=GENERIC_ERROR_MESSAGE,
        ) from None

    return report


@router.get("/export.csv")
async def export_report_csv_endpoint(
    user_id: UUID = Depends(get_current_user_id),
    pool: asyncpg.Pool | None = Depends(get_pool),
    account_id: UUID | None = Query(default=None),
    from_date: date | None = Query(default=None, alias="from"),
    to_date: date | None = Query(default=None, alias="to"),
    direction: Direction | None = None,
    currency: str | None = Query(default=None),
) -> Response:
    _require_pool(pool)

    try:
        filters, _account = await _build_filters(
            pool,
            user_id=user_id,
            account_id=account_id,
            from_date=from_date,
            to_date=to_date,
            direction=direction,
            currency=currency,
        )
        csv_rows = await get_report_csv_rows(pool, filters)
    except HTTPException:
        raise
    except Exception:
        logger.error("Unexpected error exporting report CSV for user %s", user_id, exc_info=True)
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail=GENERIC_ERROR_MESSAGE,
        ) from None

    buffer = io.StringIO()
    writer = csv.writer(buffer)
    writer.writerow(
        ["Date", "Account", "Direction", "Amount", "Currency", "Categories", "Counterparty", "Description"]
    )
    for row in csv_rows:
        writer.writerow(
            [
                row["txn_date"],
                row["account_nickname"],
                row["direction"],
                row["amount"],
                row["currency"],
                row["categories"],
                row["counterparty"],
                row["description"],
            ]
        )

    return Response(
        content=buffer.getvalue(),
        media_type="text/csv",
        headers={"Content-Disposition": 'attachment; filename="report.csv"'},
    )


@router.get("/export.pdf")
async def export_report_pdf_endpoint(
    user_id: UUID = Depends(get_current_user_id),
    pool: asyncpg.Pool | None = Depends(get_pool),
    account_id: UUID | None = Query(default=None),
    from_date: date | None = Query(default=None, alias="from"),
    to_date: date | None = Query(default=None, alias="to"),
    direction: Direction | None = None,
    currency: str | None = Query(default=None),
) -> Response:
    _require_pool(pool)

    try:
        filters, account = await _build_filters(
            pool,
            user_id=user_id,
            account_id=account_id,
            from_date=from_date,
            to_date=to_date,
            direction=direction,
            currency=currency,
        )
        rows = await fetch_report_rows(pool, filters)
        report = build_report(rows, currency=filters.currency)
        pdf_bytes = build_report_pdf(
            report,
            account_name=account["nickname"] if account else None,
            from_date=from_date,
            to_date=to_date,
            direction=direction,
        )
    except HTTPException:
        raise
    except Exception:
        logger.error("Unexpected error exporting report PDF for user %s", user_id, exc_info=True)
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail=GENERIC_ERROR_MESSAGE,
        ) from None

    return Response(
        content=pdf_bytes,
        media_type="application/pdf",
        headers={"Content-Disposition": 'attachment; filename="report.pdf"'},
    )


@router.get("/accounts", response_model=AccountSummaryResponse)
async def get_account_summary_endpoint(
    user_id: UUID = Depends(get_current_user_id),
    pool: asyncpg.Pool | None = Depends(get_pool),
    account_id: UUID | None = Query(default=None),
) -> dict:
    """Account Summary (ab-150) - every account the user has, or just the
    one matching account_id, all-time with no date dimension at all (see
    get_account_summary's own docstring)."""
    _require_pool(pool)

    try:
        items = await get_account_summary(pool, user_id=user_id, account_id=account_id)
    except HTTPException:
        raise
    except Exception:
        logger.error("Unexpected error building account summary for user %s", user_id, exc_info=True)
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail=GENERIC_ERROR_MESSAGE,
        ) from None

    return {"items": items}


@router.get("/transfers", response_model=TransferListResponse)
async def get_transfers_endpoint(
    user_id: UUID = Depends(get_current_user_id),
    pool: asyncpg.Pool | None = Depends(get_pool),
    account_id: UUID | None = Query(default=None),
    from_date: date | None = Query(default=None, alias="from"),
    to_date: date | None = Query(default=None, alias="to"),
) -> dict:
    """Account-to-Account Transfer list (ab-150) - one row per
    transfer-shaped allocation matching the filters, newest first (see
    get_transfers' own docstring)."""
    _require_pool(pool)

    try:
        items = await get_transfers(
            pool,
            user_id=user_id,
            account_id=account_id,
            from_date=from_date,
            to_date=to_date,
        )
    except HTTPException:
        raise
    except Exception:
        logger.error("Unexpected error building transfer list for user %s", user_id, exc_info=True)
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail=GENERIC_ERROR_MESSAGE,
        ) from None

    return {"items": items}


# --- Account Summary export (ab-152) ------------------------------------


@router.get("/accounts/export.csv")
async def export_account_summary_csv_endpoint(
    user_id: UUID = Depends(get_current_user_id),
    pool: asyncpg.Pool | None = Depends(get_pool),
    account_id: UUID | None = Query(default=None),
) -> Response:
    _require_pool(pool)

    try:
        items = await get_account_summary(pool, user_id=user_id, account_id=account_id)
    except HTTPException:
        raise
    except Exception:
        logger.error("Unexpected error exporting account summary CSV for user %s", user_id, exc_info=True)
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail=GENERIC_ERROR_MESSAGE,
        ) from None

    rows = [[item["account_nickname"], item["money_in"], item["money_out"]] for item in items]
    return _csv_response(["Account", "Money In", "Money Out"], rows, filename="account-summary.csv")


@router.get("/accounts/export.pdf")
async def export_account_summary_pdf_endpoint(
    user_id: UUID = Depends(get_current_user_id),
    pool: asyncpg.Pool | None = Depends(get_pool),
    account_id: UUID | None = Query(default=None),
) -> Response:
    _require_pool(pool)

    try:
        items = await get_account_summary(pool, user_id=user_id, account_id=account_id)
        account_name = None
        if account_id is not None:
            account = await get_account(pool, account_id=account_id, user_id=user_id)
            account_name = account["nickname"] if account else None
    except HTTPException:
        raise
    except Exception:
        logger.error("Unexpected error exporting account summary PDF for user %s", user_id, exc_info=True)
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail=GENERIC_ERROR_MESSAGE,
        ) from None

    rows = [[item["account_nickname"], str(item["money_in"]), str(item["money_out"])] for item in items]
    pdf_bytes = build_simple_table_pdf(
        "Account Summary", ["Account", "Money In", "Money Out"], rows, account_name=account_name
    )
    return _pdf_response(pdf_bytes, filename="account-summary.pdf")


# --- Budget Plans export (ab-152) ----------------------------------------


@router.get("/budget-plans/export.csv")
async def export_budget_plans_csv_endpoint(
    user_id: UUID = Depends(get_current_user_id),
    pool: asyncpg.Pool | None = Depends(get_pool),
    from_date: date | None = Query(default=None, alias="from"),
    to_date: date | None = Query(default=None, alias="to"),
) -> Response:
    _require_pool(pool)

    try:
        plans = await get_budget_plans_report(pool, user_id=user_id, from_date=from_date, to_date=to_date)
    except HTTPException:
        raise
    except Exception:
        logger.error("Unexpected error exporting budget plans CSV for user %s", user_id, exc_info=True)
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail=GENERIC_ERROR_MESSAGE,
        ) from None

    rows = [
        [
            plan["name"] or "—",
            _format_budget_plan_window(plan["starts_at"], plan["ends_at"]),
            plan["total_cap"] if plan["total_cap"] is not None else "—",
            plan["money_in"],
            plan["money_out"],
        ]
        for plan in plans
    ]
    return _csv_response(
        ["Plan Name", "Window (start - end)", "Total Cap", "Money In", "Money Out"],
        rows,
        filename="budget-plans.csv",
    )


@router.get("/budget-plans/export.pdf")
async def export_budget_plans_pdf_endpoint(
    user_id: UUID = Depends(get_current_user_id),
    pool: asyncpg.Pool | None = Depends(get_pool),
    from_date: date | None = Query(default=None, alias="from"),
    to_date: date | None = Query(default=None, alias="to"),
) -> Response:
    _require_pool(pool)

    try:
        plans = await get_budget_plans_report(pool, user_id=user_id, from_date=from_date, to_date=to_date)
    except HTTPException:
        raise
    except Exception:
        logger.error("Unexpected error exporting budget plans PDF for user %s", user_id, exc_info=True)
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail=GENERIC_ERROR_MESSAGE,
        ) from None

    rows = [
        [
            plan["name"] or "—",
            _format_budget_plan_window(plan["starts_at"], plan["ends_at"]),
            str(plan["total_cap"]) if plan["total_cap"] is not None else "—",
            str(plan["money_in"]),
            str(plan["money_out"]),
        ]
        for plan in plans
    ]
    pdf_bytes = build_simple_table_pdf(
        "Budget Plans",
        ["Plan Name", "Window (start - end)", "Total Cap", "Money In", "Money Out"],
        rows,
        from_date=from_date,
        to_date=to_date,
    )
    return _pdf_response(pdf_bytes, filename="budget-plans.pdf")


# --- Category Summary export (ab-152, shared by Expenses/Income) --------


@router.get("/categories/export.csv")
async def export_category_summary_csv_endpoint(
    category_type: CategoryType,
    user_id: UUID = Depends(get_current_user_id),
    pool: asyncpg.Pool | None = Depends(get_pool),
    from_date: date | None = Query(default=None, alias="from"),
    to_date: date | None = Query(default=None, alias="to"),
) -> Response:
    _require_pool(pool)

    try:
        filters, _account = await _build_filters(
            pool,
            user_id=user_id,
            account_id=None,
            from_date=from_date,
            to_date=to_date,
            direction=None,
            currency=None,
        )
        items = await get_category_summary(pool, filters, category_type=category_type)
    except HTTPException:
        raise
    except Exception:
        logger.error("Unexpected error exporting category summary CSV for user %s", user_id, exc_info=True)
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail=GENERIC_ERROR_MESSAGE,
        ) from None

    rows = [[item["category_name"] or "—", item["total"]] for item in items]
    return _csv_response(
        ["Category", "Amount"],
        rows,
        filename=f"{category_type}-summary.csv",
    )


@router.get("/categories/export.pdf")
async def export_category_summary_pdf_endpoint(
    category_type: CategoryType,
    user_id: UUID = Depends(get_current_user_id),
    pool: asyncpg.Pool | None = Depends(get_pool),
    from_date: date | None = Query(default=None, alias="from"),
    to_date: date | None = Query(default=None, alias="to"),
) -> Response:
    _require_pool(pool)

    try:
        filters, _account = await _build_filters(
            pool,
            user_id=user_id,
            account_id=None,
            from_date=from_date,
            to_date=to_date,
            direction=None,
            currency=None,
        )
        items = await get_category_summary(pool, filters, category_type=category_type)
    except HTTPException:
        raise
    except Exception:
        logger.error("Unexpected error exporting category summary PDF for user %s", user_id, exc_info=True)
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail=GENERIC_ERROR_MESSAGE,
        ) from None

    rows = [[item["category_name"] or "—", str(item["total"])] for item in items]
    title = "Expense Summary" if category_type == "expense" else "Income Summary"
    pdf_bytes = build_simple_table_pdf(
        title, ["Category", "Amount"], rows, from_date=from_date, to_date=to_date
    )
    return _pdf_response(pdf_bytes, filename=f"{category_type}-summary.pdf")


# --- Transfers export (ab-152) -------------------------------------------


@router.get("/transfers/export.csv")
async def export_transfers_csv_endpoint(
    user_id: UUID = Depends(get_current_user_id),
    pool: asyncpg.Pool | None = Depends(get_pool),
    account_id: UUID | None = Query(default=None),
    from_date: date | None = Query(default=None, alias="from"),
    to_date: date | None = Query(default=None, alias="to"),
) -> Response:
    _require_pool(pool)

    try:
        items = await get_transfers(
            pool,
            user_id=user_id,
            account_id=account_id,
            from_date=from_date,
            to_date=to_date,
        )
    except HTTPException:
        raise
    except Exception:
        logger.error("Unexpected error exporting transfers CSV for user %s", user_id, exc_info=True)
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail=GENERIC_ERROR_MESSAGE,
        ) from None

    rows = [
        [
            item["transfer_reason_name"] or "—",
            item["source_label"],
            item["destination_account_name"],
            item["amount"],
            item["date"],
        ]
        for item in items
    ]
    return _csv_response(
        ["Transfer Nature", "Source Account", "Destination Account", "Amount", "Date"],
        rows,
        filename="transfers.csv",
    )


@router.get("/transfers/export.pdf")
async def export_transfers_pdf_endpoint(
    user_id: UUID = Depends(get_current_user_id),
    pool: asyncpg.Pool | None = Depends(get_pool),
    account_id: UUID | None = Query(default=None),
    from_date: date | None = Query(default=None, alias="from"),
    to_date: date | None = Query(default=None, alias="to"),
) -> Response:
    _require_pool(pool)

    try:
        items = await get_transfers(
            pool,
            user_id=user_id,
            account_id=account_id,
            from_date=from_date,
            to_date=to_date,
        )
        account_name = None
        if account_id is not None:
            account = await get_account(pool, account_id=account_id, user_id=user_id)
            account_name = account["nickname"] if account else None
    except HTTPException:
        raise
    except Exception:
        logger.error("Unexpected error exporting transfers PDF for user %s", user_id, exc_info=True)
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail=GENERIC_ERROR_MESSAGE,
        ) from None

    rows = [
        [
            item["transfer_reason_name"] or "—",
            item["source_label"],
            item["destination_account_name"],
            str(item["amount"]),
            str(item["date"]),
        ]
        for item in items
    ]
    pdf_bytes = build_simple_table_pdf(
        "Transfers Summary",
        ["Transfer Nature", "Source Account", "Destination Account", "Amount", "Date"],
        rows,
        account_name=account_name,
        from_date=from_date,
        to_date=to_date,
    )
    return _pdf_response(pdf_bytes, filename="transfers.pdf")
