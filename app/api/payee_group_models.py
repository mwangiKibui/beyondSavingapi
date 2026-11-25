"""Response shapes and list parameters shared by the payee-group endpoints
(ab-282): Transactions -> Uncategorised and an import's review tabs."""

from datetime import date, datetime
from typing import Literal
from uuid import UUID

from fastapi import HTTPException, Query, status
from pydantic import BaseModel

from app.services.payee_groups import ALLOWED_PAGE_SIZES, DEFAULT_PAGE_SIZE


class GroupStatement(BaseModel):
    import_id: UUID
    file_name: str | None = None
    account_nickname: str | None = None


class GroupRow(BaseModel):
    transaction_id: UUID
    txn_date: date
    amount: float
    currency: str
    description: str | None = None
    import_id: UUID | None = None
    # ab-296: the upload that first recorded the row (import_id's file and
    # when it was uploaded) - a re-upload's rows were recorded earlier.
    file_name: str | None = None
    imported_at: datetime | None = None
    # An import's review (ab-292): this line's own status, category, apply
    # and suggestion.
    status: Literal["decide", "decided", "sorted", "handled", "accepted"] | None = None
    category_id: UUID | None = None
    category_name: str | None = None
    transfer_reason_id: UUID | None = None
    transfer_reason_name: str | None = None
    # ab-286: filed as a refund of category_name (an expense category).
    is_refund: bool = False
    # ab-288: expense, income, refund, transfer, saving, borrowed or
    # loan_repayment once filed; None while open (or mixed, for a group).
    kind: str | None = None
    account_id: UUID | None = None
    sub_ledger_id: UUID | None = None
    sub_ledger_name: str | None = None
    sub_ledger_role: str | None = None
    # A transfer's other side (account nickname or the name the user gave).
    other_side: str | None = None
    # ab-288: the user's own words for the row ("Other" + a note).
    note: str | None = None
    apply_id: UUID | None = None
    suggestion: "GroupSuggestion | None" = None


class GroupSuggestion(BaseModel):
    # None when is_new_category: applying it creates the category.
    category_id: UUID | None = None
    category_name: str
    is_new_category: bool
    # own_account (ab-285): money in sent by the user themselves.
    source: Literal["your_rule", "this_statement", "others", "directory", "keyword", "own_account", "ai"]
    merchant_name: str | None = None
    # ab-290: the description keyword behind it ("salary"), if any.
    keyword: str | None = None
    # ab-285: "transfer" = file as a Funds transfer; category_name then
    # holds the reason's name, and transfer_reason_id is None when the
    # reason gets created on approval.
    kind: Literal["category", "transfer"] = "category"
    transfer_reason_id: UUID | None = None


class PayeeGroup(BaseModel):
    # Opaque; names, numbers and references never appear in it.
    group_key: str
    # Person rows show the statement text - only ever to the importer (ab-269).
    display_name: str
    label: str | None = None
    key_kind: str | None = None
    number_kind: str | None = None
    number: str | None = None
    is_person: bool
    is_business: bool
    direction: Literal["in", "out"]
    currency: str
    row_count: int
    total: float
    first_date: date
    last_date: date
    statements: list[GroupStatement]
    suggestion: GroupSuggestion | None = None
    # Set when every row has the same single category / was filed by the
    # same apply (undo it with DELETE /category-applies/{apply_id}).
    category_id: UUID | None = None
    category_name: str | None = None
    # ab-285: every row filed as a Funds transfer under this reason.
    transfer_reason_id: UUID | None = None
    transfer_reason_name: str | None = None
    is_refund: bool = False
    # ab-288: expense, income, refund, transfer, saving, borrowed or
    # loan_repayment once filed; None while open (or mixed, for a group).
    kind: str | None = None
    sub_ledger_id: UUID | None = None
    sub_ledger_name: str | None = None
    sub_ledger_role: str | None = None
    # A transfer's other side (account nickname or the name the user gave).
    other_side: str | None = None
    apply_id: UUID | None = None
    # Uncategorised -> "Categorised here" only (ab-284): when it was filed.
    applied_at: datetime | None = None
    # Handled tab only (ab-281).
    handled_as: Literal["charge", "loan", "transfer", "saving"] | None = None
    handled_reason: str | None = None
    # An import's review only (ab-289): decide = Pending, handled =
    # Auto-suggested, decided/sorted = Approved (here / some other way).
    status: Literal["decide", "decided", "sorted", "handled", "accepted"] | None = None
    pending_count: int | None = None
    approved_count: int | None = None
    rows: list[GroupRow]


Direction = Literal["in", "out"]
Kind = Literal["person", "business"]
Sort = Literal["rows", "amount", "payee", "recent"]
Order = Literal["asc", "desc"]


class ListParams(BaseModel):
    direction: Direction | None = None
    kind: Kind | None = None
    q: str | None = None
    sort: Sort = "rows"
    order: Order = "desc"
    page: int = 1
    page_size: int = DEFAULT_PAGE_SIZE


def list_params(
    direction: Direction | None = Query(default=None),
    kind: Kind | None = Query(default=None),
    # Search: payee name, label, number or statement text.
    q: str | None = Query(default=None, max_length=100),
    sort: Sort = Query(default="rows"),
    order: Order = Query(default="desc"),
    page: int = Query(default=1, ge=1),
    page_size: int = Query(default=DEFAULT_PAGE_SIZE),
) -> ListParams:
    return ListParams(direction=direction, kind=kind, q=q, sort=sort, order=order, page=page, page_size=page_size)


def check_page_size(params: ListParams) -> None:
    """Called in the handler, not in the Depends() above (raising there
    breaks FastAPI's validation ordering)."""
    if params.page_size not in ALLOWED_PAGE_SIZES:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail=f"page_size must be one of {', '.join(map(str, ALLOWED_PAGE_SIZES))}",
        )
