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
    get_report,
    get_report_csv_rows,
    resolve_report_currency,
)
from app.services.report_pdf import build_report_pdf

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/reports", tags=["reports"])

# Matches transactions.py's list endpoint convention: the ticket wording
# calls this filter "type", but this codebase's established name
# everywhere else is "direction" - kept consistent here rather than
# introducing a second name for the same thing.
Direction = Literal["in", "out"]


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


def _require_pool(pool: asyncpg.Pool | None) -> None:
    if pool is None:
        logger.error("Database pool unavailable")
        raise HTTPException(status_code=status.HTTP_503_SERVICE_UNAVAILABLE, detail=GENERIC_ERROR_MESSAGE)


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
