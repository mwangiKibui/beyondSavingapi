"""Repository-level tests for app/repositories/dashboard.py's own Python
aggregation (build_dashboard) - exercised directly against hand-built
fetch_dashboard_rows()-shaped row lists rather than a mocked pool, same
convention as test_reports_aggregation.py.
"""

from datetime import date, datetime, timezone
from decimal import Decimal
from uuid import uuid4

from app.repositories.dashboard import build_dashboard, get_dashboard

ZERO = Decimal("0.00")


def _row(**overrides):
    base = {
        "account_id": None,
        "account_nickname": "KCB Salary",
        "transaction_id": None,
        "txn_amount": None,
        "direction": None,
        "has_transfer_allocation": False,
    }
    base.update(overrides)
    return base


def test_build_dashboard_includes_a_zero_activity_account():
    account_id = uuid4()
    rows = [_row(account_id=account_id, account_nickname="Empty Account")]

    dashboard = build_dashboard(rows, total_budget=Decimal("5000.00"), currency="KES")

    assert dashboard["currency"] == "KES"
    assert dashboard["total_budget"] == Decimal("5000.00")
    assert dashboard["total_in"] == ZERO
    assert dashboard["total_out"] == ZERO
    assert dashboard["net"] == ZERO
    assert dashboard["accounts"] == [
        {
            "account_id": account_id,
            "account_nickname": "Empty Account",
            "money_in": ZERO,
            "money_out": ZERO,
        }
    ]


def test_build_dashboard_sums_money_in_and_out_per_account_and_overall():
    acc1 = uuid4()
    acc2 = uuid4()
    rows = [
        _row(account_id=acc1, account_nickname="Salary Account", transaction_id=uuid4(), txn_amount=Decimal("1000.00"), direction="in"),
        _row(account_id=acc1, account_nickname="Salary Account", transaction_id=uuid4(), txn_amount=Decimal("200.00"), direction="out"),
        _row(account_id=acc2, account_nickname="Mpesa", transaction_id=uuid4(), txn_amount=Decimal("500.00"), direction="in"),
    ]

    dashboard = build_dashboard(rows, total_budget=ZERO, currency="KES")

    assert dashboard["total_in"] == Decimal("1500.00")
    assert dashboard["total_out"] == Decimal("200.00")
    assert dashboard["net"] == Decimal("1300.00")
    accounts_by_name = {acc["account_nickname"]: acc for acc in dashboard["accounts"]}
    assert accounts_by_name["Salary Account"]["money_in"] == Decimal("1000.00")
    assert accounts_by_name["Salary Account"]["money_out"] == Decimal("200.00")
    assert accounts_by_name["Mpesa"]["money_in"] == Decimal("500.00")


def test_build_dashboard_excludes_a_transaction_with_a_transfer_shaped_allocation():
    """Matches build_report's own total_in/total_out convention - the
    WHOLE transaction nets out, even the portion not covered by the
    transfer allocation, same as every other report in this codebase."""
    account_id = uuid4()
    rows = [
        _row(account_id=account_id, transaction_id=uuid4(), txn_amount=Decimal("1000.00"), direction="in"),
        _row(
            account_id=account_id,
            transaction_id=uuid4(),
            txn_amount=Decimal("300.00"),
            direction="out",
            has_transfer_allocation=True,
        ),
    ]

    dashboard = build_dashboard(rows, total_budget=ZERO, currency="KES")

    assert dashboard["total_in"] == Decimal("1000.00")
    assert dashboard["total_out"] == ZERO
    assert dashboard["accounts"][0]["money_out"] == ZERO


def test_build_dashboard_sorts_accounts_by_nickname():
    rows = [
        _row(account_id=uuid4(), account_nickname="Zebra Savings"),
        _row(account_id=uuid4(), account_nickname="Alpha Checking"),
    ]

    dashboard = build_dashboard(rows, total_budget=ZERO, currency="KES")

    assert [acc["account_nickname"] for acc in dashboard["accounts"]] == ["Alpha Checking", "Zebra Savings"]


class _FakeDashboardPool:
    """Differentiates fetch_budget_plan_rows' query (SELECT ... FROM
    budget_plans) from fetch_dashboard_rows' own (SELECT ... FROM
    accounts) by a substring check, since get_dashboard makes both calls
    against the same pool - everything else about each aggregation is
    already covered by filter_budget_plans_by_window's own tests and
    build_dashboard's tests above, so this only proves get_dashboard
    wires currency + window-overlap filtering together correctly for
    total_budget.
    """

    def __init__(self, plan_rows):
        self._plan_rows = plan_rows

    async def fetch(self, query, *args):
        if "FROM budget_plans" in query:
            return self._plan_rows
        return []


async def test_get_dashboard_sums_only_overlapping_same_currency_budget_plans():
    plan_rows = [
        {
            "id": uuid4(),
            "name": "September plan",
            "starts_at": datetime(2026, 9, 1, tzinfo=timezone.utc),
            "ends_at": datetime(2026, 9, 30, tzinfo=timezone.utc),
            "total_cap": Decimal("20000.00"),
            "currency": "KES",
        },
        {
            "id": uuid4(),
            "name": "October plan",
            "starts_at": datetime(2026, 10, 1, tzinfo=timezone.utc),
            "ends_at": datetime(2026, 10, 31, tzinfo=timezone.utc),
            "total_cap": Decimal("15000.00"),
            "currency": "KES",
        },
        {
            "id": uuid4(),
            "name": "Different currency plan",
            "starts_at": datetime(2026, 9, 1, tzinfo=timezone.utc),
            "ends_at": datetime(2026, 9, 30, tzinfo=timezone.utc),
            "total_cap": Decimal("999.00"),
            "currency": "USD",
        },
        {
            "id": uuid4(),
            "name": "No cap set",
            "starts_at": datetime(2026, 9, 1, tzinfo=timezone.utc),
            "ends_at": datetime(2026, 9, 30, tzinfo=timezone.utc),
            "total_cap": None,
            "currency": "KES",
        },
    ]
    pool = _FakeDashboardPool(plan_rows)

    dashboard = await get_dashboard(
        pool, user_id=uuid4(), currency="KES", from_date=date(2026, 9, 1), to_date=date(2026, 9, 30)
    )

    # Only "September plan" overlaps the window, is KES, and has a cap -
    # "October plan" doesn't overlap, "Different currency plan" is USD,
    # "No cap set" has nothing to add.
    assert dashboard["total_budget"] == Decimal("20000.00")
