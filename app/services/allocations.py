"""Decisions shared by bulk allocation (ab-58/59/60) and the statement
import confirm step (ab-253). Pure: callers fetch the transactions and
categories (on whatever pool/connection they use) and do the writes."""

from dataclasses import dataclass, field
from decimal import Decimal
from uuid import UUID

# expense categories only ever apply to a money-out transaction, income
# only to money-in (ab-60) - category_id None (reconciled, no category,
# ab-124) skips the check, since there's no type to compare.
CATEGORY_TYPE_FOR_DIRECTION = {"out": "expense", "in": "income"}


class AllocationRejected(Exception):
    """A request-level rejection; the caller turns it into an HTTP error."""

    def __init__(self, status_code: int, detail: str):
        super().__init__(detail)
        self.status_code = status_code
        self.detail = detail


@dataclass
class AllocationPlan:
    transaction_ids: list[UUID] = field(default_factory=list)
    category_ids: list[UUID | None] = field(default_factory=list)
    amounts: list[float] = field(default_factory=list)
    currencies: list[str] = field(default_factory=list)
    updated: list[UUID] = field(default_factory=list)
    skipped: list[UUID] = field(default_factory=list)


def plan_bulk_allocation(
    category_by_transaction: dict[UUID, UUID | None],
    *,
    transactions_by_id: dict[UUID, dict],
    categories_by_id: dict[UUID, dict],
) -> AllocationPlan:
    """All checks run before anything is planned - one bad pair rejects
    the whole request. Each transaction gets exactly its own remaining
    balance (ab-59); nothing remaining means skipped, not an error."""
    if category_by_transaction.keys() - transactions_by_id.keys():
        raise AllocationRejected(404, "Transaction not found")
    wanted = {cid for cid in category_by_transaction.values() if cid is not None}
    if wanted - categories_by_id.keys():
        raise AllocationRejected(404, "Category not found")

    for transaction_id, category_id in category_by_transaction.items():
        if category_id is None:
            continue
        expected_type = CATEGORY_TYPE_FOR_DIRECTION[transactions_by_id[transaction_id]["direction"]]
        if categories_by_id[category_id]["type"] != expected_type:
            raise AllocationRejected(
                422, "A category's type doesn't match one of the selected transactions' direction"
            )

    plan = AllocationPlan()
    for transaction_id, category_id in category_by_transaction.items():
        transaction = transactions_by_id[transaction_id]
        remaining = float(transaction["amount"]) - float(transaction["allocated"])
        if remaining <= 0:
            plan.skipped.append(transaction_id)
            continue
        plan.transaction_ids.append(transaction_id)
        plan.category_ids.append(category_id)
        plan.amounts.append(remaining)
        plan.currencies.append(transaction["currency"])
        plan.updated.append(transaction_id)
    return plan


def alert_groups(plan: AllocationPlan, transactions_by_id: dict[UUID, dict]) -> dict[tuple, Decimal]:
    """ab-77: one budget-alert check per (category_id, txn_date, currency).
    txn_date can be absent from a transaction row a test mocked directly;
    that row is skipped rather than raising."""
    groups: dict[tuple, Decimal] = {}
    for txn_id, category_id, amount, currency in zip(
        plan.transaction_ids, plan.category_ids, plan.amounts, plan.currencies
    ):
        if category_id is None:
            continue
        txn_date = transactions_by_id[txn_id].get("txn_date")
        if txn_date is None:
            continue
        key = (category_id, txn_date, currency)
        groups[key] = groups.get(key, Decimal(0)) + Decimal(str(amount))
    return groups
