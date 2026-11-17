"""Budget plan totals and money flows only ever read the signed-in user's
own allocations (allocations has no user_id; ownership comes from the
transaction's account). Before this fix they summed - and listed - every
user's rows in the plan's currency and window."""

from datetime import date
from decimal import Decimal
from unittest.mock import AsyncMock, patch
from uuid import uuid4

import pytest

from app.repositories import budget_plans, reports

USER = uuid4()
PLAN = {
    "id": uuid4(),
    "name": "November",
    "period": "monthly",
    "starts_at": date(2026, 11, 1),
    "ends_at": date(2026, 11, 30),
    "total_cap": None,
    "currency": "KES",
    "is_recurring": False,
    "is_active": True,
    "created_at": None,
}


class RecordingPool:
    """Records every query and its arguments; returns canned results."""

    def __init__(self, fetch=None, fetchrow=None, fetchval=None):
        self.calls = []
        self._fetch = fetch or (lambda sql, args: [])
        self._fetchrow = fetchrow or (lambda sql, args: {"total_income": Decimal("0"), "total_expenditure": Decimal("0")})
        self._fetchval = fetchval or (lambda sql, args: None)

    async def fetch(self, sql, *args):
        self.calls.append((sql, args))
        return self._fetch(sql, args)

    async def fetchrow(self, sql, *args):
        self.calls.append((sql, args))
        return self._fetchrow(sql, args)

    async def fetchval(self, sql, *args):
        self.calls.append((sql, args))
        return self._fetchval(sql, args)


def allocation_queries(pool):
    return [(sql, args) for sql, args in pool.calls if "FROM allocations a" in sql]


def assert_user_scoped(sql, args):
    assert "JOIN accounts acc ON acc.id = t.account_id" in sql
    assert "acc.user_id = $" in sql
    assert USER in args


def assert_counted_only(sql):
    assert "a.transfer_reason_id IS NULL" in sql
    assert "NOT (t.handled_as = 'loan' AND a.category_id IS NULL)" in sql


@pytest.mark.asyncio
async def test_plan_totals_reads_only_the_users_allocations():
    pool = RecordingPool(fetchrow=lambda sql, args: {"total_income": Decimal("50"), "total_expenditure": Decimal("20")})

    totals = await budget_plans.plan_totals(
        pool, user_id=USER, currency="KES", starts_at=PLAN["starts_at"], ends_at=PLAN["ends_at"]
    )

    assert totals == (Decimal("50"), Decimal("20"))
    sql, args = pool.calls[0]
    assert_user_scoped(sql, args)
    assert_counted_only(sql)
    assert args == (USER, "KES", PLAN["starts_at"], PLAN["ends_at"])


@pytest.mark.asyncio
async def test_money_flows_list_only_the_users_rows():
    pool = RecordingPool()
    with patch.object(budget_plans, "get_budget_plan", AsyncMock(return_value=PLAN)):
        flows = await budget_plans.list_plan_money_flows(pool, user_id=USER, plan_id=PLAN["id"])

    assert flows == {"money_in": [], "money_out": []}
    queries = allocation_queries(pool)
    assert len(queries) == 2
    for sql, args in queries:
        assert_user_scoped(sql, args)
        assert_counted_only(sql)
    assert {args[2] for _, args in queries} == {"in", "out"}


@pytest.mark.asyncio
async def test_list_budget_plans_scopes_totals_and_consumption():
    def fetch(sql, args):
        if "FROM budget_plans" in sql:
            return [PLAN]
        if "FROM budgets b" in sql:
            return [{"id": uuid4(), "category_id": uuid4(), "category_name": "Groceries", "category_type": "expense",
                     "limit_amount": Decimal("100"), "consumed": Decimal("10")}]
        return []

    pool = RecordingPool(fetch=fetch, fetchval=lambda sql, args: Decimal("0.80"))
    plans = await budget_plans.list_budget_plans(pool, user_id=USER)

    assert len(plans) == 1
    for sql, args in allocation_queries(pool) + [c for c in pool.calls if "FROM budgets b" in c[0]]:
        assert_user_scoped(sql, args)


@pytest.mark.asyncio
async def test_budget_plans_export_uses_the_scoped_totals():
    pool = RecordingPool()
    with patch.object(reports, "fetch_budget_plan_rows", AsyncMock(return_value=[PLAN])):
        result = await reports.get_budget_plans_report(pool, user_id=USER, from_date=None, to_date=None)

    assert result[0]["money_in"] == Decimal("0") and result[0]["money_out"] == Decimal("0")
    queries = allocation_queries(pool)
    assert len(queries) == 1
    assert_user_scoped(*queries[0])
