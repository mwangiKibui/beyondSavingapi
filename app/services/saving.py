"""How much a transaction adds to the user's savings (ab-259) - the
Saving bucket next to income and spending in reports, the dashboard and
budget plans. Positive = money into savings, negative = money out.

- A row handled as saving (app.services.row_handling) on a locked-savings
  or shares sub-ledger: a credit is saved, a debit leaves savings.
- A row handled as saving elsewhere (the withdrawable account's side of a
  move into deposits, when the deposits side isn't in): a debit is saved,
  a credit came back out.
- Spending filed under a saving category (categories.entry_type, e.g. a
  Paybill to a SACCO or money-market fund the user doesn't track): saved.

Saving is never income or spending."""

from decimal import Decimal

from app.services.sub_ledger_roles import SAVING_ROLES

ZERO = Decimal("0.00")


def handled_saving(*, direction: str, sub_ledger_role: str | None, amount: Decimal) -> Decimal:
    on_saving_ledger = sub_ledger_role in SAVING_ROLES
    return amount if (direction == "in") == on_saving_ledger else -amount


# The same rule in SQL, for queries that total in the database. Expects
# transactions as t and its sub-ledger (LEFT JOINed) as sl.
HANDLED_SAVING_SQL = (
    "CASE WHEN (t.direction = 'in') = COALESCE(sl.role IN ('locked_saving', 'shares'), false)"
    " THEN t.amount ELSE -t.amount END"
)
