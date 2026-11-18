"""What a row's money is (ab-287) - one definition every report, the
dashboard, budget plans and budget alerts use, instead of each checking
handled_as / categories.entry_type / transfer_reason_id on its own.

A kind is derived, never stored: it comes from

  direction            transactions.direction (in / out)
  handled at import    transactions.handled_as (charge, loan, transfer, saving)
  category type        categories.type (income / expense)
  category meaning     categories.entry_type (general, saving, loan_repayment)
  category present     allocations.category_id
  transfer             allocations.transfer_reason_id

Kinds, by precedence (allocation_kind):

  transfer         the allocation has a transfer reason
  saving           the row was handled as saving at import (ab-259)
  borrowed         a loan row handled at import with no category (ab-281)
  refund           money in filed under an EXPENSE category (ab-286) - it
                   lowers that category's spending; never income
  saving           money out filed under a saving category
  loan_repayment   money out filed under a loan-repayment category
  spending         any other money out
  income           any other money in

Where each kind counts (BUCKET): spending and loan_repayment are money
out, a refund is money out taken back (SIGN -1), income is money in,
saving is the Saving bucket (never income or spending), borrowed and
transfer count nowhere.

Reports that total whole transactions (GET /reports, the dashboard) use
transaction_kind: a transaction with ANY transfer allocation is netted out
whole (ab-79), as is a handled loan with no category anywhere."""

from decimal import Decimal
from typing import Literal

from app.services.sub_ledger_roles import SAVING_ROLES

ZERO = Decimal("0.00")

RowKind = Literal["spending", "income", "saving", "loan_repayment", "borrowed", "transfer", "refund"]
KINDS: tuple[str, ...] = ("spending", "income", "saving", "loan_repayment", "borrowed", "transfer", "refund")

BUCKET: dict[str, str | None] = {
    "spending": "out",
    "loan_repayment": "out",
    "income": "in",
    "saving": "saving",
    "borrowed": None,
    "transfer": None,
    "refund": "out",  # ab-286: counted as negative spending (SIGN)
}
# A refund takes spending back.
SIGN: dict[str, int] = {k: (-1 if k == "refund" else 1) for k in BUCKET}
# Kinds counted as income / spending in totals.
COUNTED_KINDS = frozenset(k for k, b in BUCKET.items() if b in ("in", "out"))

TransactionKind = Literal["transfer", "borrowed", "saving", "counted"]


def allocation_kind(
    *,
    direction: str,
    handled_as: str | None,
    has_category: bool,
    entry_type: str | None,
    has_transfer_reason: bool,
    category_type: str | None = None,
) -> RowKind:
    if has_transfer_reason:
        return "transfer"
    if handled_as == "saving":
        return "saving"
    if handled_as == "loan" and not has_category:
        return "borrowed"
    if direction == "in" and category_type == "expense":
        return "refund"
    if direction == "out":
        if entry_type == "saving":
            return "saving"
        if entry_type == "loan_repayment":
            return "loan_repayment"
        return "spending"
    return "income"


def kind_of_row(row: dict) -> RowKind:
    """allocation_kind for a joined (transaction, allocation, category) row
    with keys direction, handled_as, category_id, category_type,
    category_entry_type, transfer_reason_id."""
    return allocation_kind(
        direction=row["direction"],
        handled_as=row.get("handled_as"),
        has_category=row.get("category_id") is not None,
        entry_type=row.get("category_entry_type"),
        has_transfer_reason=row.get("transfer_reason_id") is not None,
        category_type=row.get("category_type"),
    )


def counted_amount(kind: str, amount: Decimal) -> Decimal:
    """An allocation's amount as it counts in its bucket: a refund
    negative (ab-286)."""
    return amount * SIGN[kind]


def transaction_refund(alloc_rows: list[dict]) -> Decimal:
    """The part of a money-in transaction filed as a refund (ab-286): it
    comes off money out instead of counting as money in."""
    return sum(
        (r["alloc_amount"] for r in alloc_rows if r["allocation_id"] is not None and kind_of_row(r) == "refund"),
        ZERO,
    )


def transaction_kind(alloc_rows: list[dict]) -> TransactionKind:
    """A whole transaction, from its allocation rows (each carrying the
    transaction's handled_as): transfer if any allocation is one, borrowed
    if it's a handled loan with no category anywhere, saving if handled as
    saving, else counted (income/spending, with any saving part split off
    by transaction_saving)."""
    if any(r.get("transfer_reason_id") is not None for r in alloc_rows):
        return "transfer"
    handled_as = alloc_rows[0].get("handled_as")
    if handled_as == "loan" and not any(r.get("category_id") is not None for r in alloc_rows):
        return "borrowed"
    if handled_as == "saving":
        return "saving"
    return "counted"


def handled_saving(*, direction: str, sub_ledger_role: str | None, amount: Decimal) -> Decimal:
    """A row handled as saving (ab-259): on a locked-savings or shares
    sub-ledger a credit is saved and a debit leaves savings; elsewhere (the
    withdrawable side of a move into deposits) a debit is saved and a
    credit came back out."""
    on_saving_ledger = sub_ledger_role in SAVING_ROLES
    return amount if (direction == "in") == on_saving_ledger else -amount


def saved_amount(*, handled_as: str | None, direction: str, sub_ledger_role: str | None, amount: Decimal) -> Decimal:
    """What a saving-kind amount adds to savings: signed for a row handled
    as saving, as-is for money out under a saving category."""
    if handled_as == "saving":
        return handled_saving(direction=direction, sub_ledger_role=sub_ledger_role, amount=amount)
    return amount


def transaction_saving(alloc_rows: list[dict]) -> Decimal:
    """What one transaction adds to savings: the whole row when handled as
    saving, else the part of money out filed under a saving category.
    alloc_rows carry txn_amount, direction, handled_as, sub_ledger_role and
    per allocation allocation_id, alloc_amount, category_entry_type."""
    first = alloc_rows[0]
    if first.get("handled_as") == "saving":
        return handled_saving(
            direction=first["direction"], sub_ledger_role=first.get("sub_ledger_role"), amount=first["txn_amount"]
        )
    if first["direction"] != "out":
        return ZERO
    return sum(
        (
            r["alloc_amount"]
            for r in alloc_rows
            if r["allocation_id"] is not None and r.get("transfer_reason_id") is None
            and r.get("category_entry_type") == "saving"
        ),
        ZERO,
    )


# --- the same rules in SQL ---------------------------------------------------
# Each snippet names the aliases it expects.

# transactions t + its sub-ledger sl (LEFT JOINed).
HANDLED_SAVING_SQL = (
    "CASE WHEN (t.direction = 'in') = COALESCE(sl.role IN ('locked_saving', 'shares'), false)"
    " THEN t.amount ELSE -t.amount END"
)


def allocation_kind_sql(*, t: str = "t", a: str = "a", c: str = "c") -> str:
    """allocation_kind as a CASE over transactions {t}, allocations {a} and
    the allocation's category {c} (LEFT JOINed)."""
    return (
        f"(CASE WHEN {a}.transfer_reason_id IS NOT NULL THEN 'transfer'"
        f" WHEN {t}.handled_as = 'saving' THEN 'saving'"
        f" WHEN {t}.handled_as = 'loan' AND {a}.category_id IS NULL THEN 'borrowed'"
        f" WHEN {t}.direction = 'in' AND {c}.type = 'expense' THEN 'refund'"
        f" WHEN {t}.direction = 'out' AND {c}.entry_type = 'saving' THEN 'saving'"
        f" WHEN {t}.direction = 'out' AND {c}.entry_type = 'loan_repayment' THEN 'loan_repayment'"
        f" WHEN {t}.direction = 'out' THEN 'spending'"
        " ELSE 'income' END)"
    )


ALLOCATION_KIND_SQL = allocation_kind_sql()
# Allocations counted as income / spending (refunds included).
COUNTED_ALLOCATION_SQL = (
    f"{ALLOCATION_KIND_SQL} IN ({', '.join(repr(k) for k in sorted(COUNTED_KINDS))})"
)


def income_amount_sql(*, t: str = "t", a: str = "a", c: str = "c") -> str:
    """What an allocation adds to income (0 when it isn't income)."""
    return f"(CASE WHEN {allocation_kind_sql(t=t, a=a, c=c)} = 'income' THEN {a}.amount ELSE 0 END)"


def spending_amount_sql(*, t: str = "t", a: str = "a", c: str = "c") -> str:
    """What an allocation adds to spending: a refund takes it back
    (ab-286); 0 for anything that isn't spending."""
    kind = allocation_kind_sql(t=t, a=a, c=c)
    return (
        f"(CASE WHEN {kind} IN ('spending', 'loan_repayment') THEN {a}.amount"
        f" WHEN {kind} = 'refund' THEN -{a}.amount ELSE 0 END)"
    )


def category_spend_sql(*, t: str = "t", a: str = "a", c: str = "c") -> str:
    """A category's spend for budgets and alerts: its allocations, refunds
    taken off (ab-286)."""
    return f"(CASE WHEN {allocation_kind_sql(t=t, a=a, c=c)} = 'refund' THEN -{a}.amount ELSE {a}.amount END)"
# What an allocation adds to savings (t, a, c, sl): a handled-saving row
# counts the whole transaction, signed; a saving category its amount.
SAVED_ALLOCATION_SQL = (
    f"CASE WHEN t.handled_as = 'saving' THEN {HANDLED_SAVING_SQL}"
    f" WHEN {ALLOCATION_KIND_SQL} = 'saving' THEN a.amount END"
)


def is_transfer_allocation_sql(a: str = "a") -> str:
    """An allocation {a} of kind transfer."""
    return f"{a}.transfer_reason_id IS NOT NULL"


# transaction_kind over transactions t (allocations looked up per row).
TRANSACTION_KIND_SQL = (
    "(CASE WHEN EXISTS (SELECT 1 FROM allocations al WHERE al.transaction_id = t.id"
    " AND al.transfer_reason_id IS NOT NULL) THEN 'transfer'"
    " WHEN t.handled_as = 'loan' AND NOT EXISTS (SELECT 1 FROM allocations al"
    " WHERE al.transaction_id = t.id AND al.category_id IS NOT NULL) THEN 'borrowed'"
    " WHEN t.handled_as = 'saving' THEN 'saving'"
    " ELSE 'counted' END)"
)
# transaction_refund over transactions t: money in filed under expense
# categories (ab-286).
TRANSACTION_REFUND_SQL = (
    "(CASE WHEN t.direction = 'in' AND t.handled_as IS DISTINCT FROM 'saving'"
    " THEN COALESCE((SELECT SUM(al.amount) FROM allocations al JOIN categories c ON c.id = al.category_id"
    " WHERE al.transaction_id = t.id AND c.type = 'expense'), 0) ELSE 0 END)"
)
# transaction_saving over transactions t + sub-ledger sl.
TRANSACTION_SAVING_SQL = (
    f"(CASE WHEN t.handled_as = 'saving' THEN {HANDLED_SAVING_SQL}"
    " WHEN t.direction = 'out' THEN COALESCE((SELECT SUM(al.amount) FROM allocations al"
    " JOIN categories c ON c.id = al.category_id"
    " WHERE al.transaction_id = t.id AND c.entry_type = 'saving'), 0)"
    " ELSE 0 END)"
)
