from datetime import date
from decimal import Decimal
from unittest.mock import AsyncMock
from uuid import uuid4

import pytest
from fastapi.testclient import TestClient

from app.core.db import get_pool
from app.core.security import get_current_user_id
from app.main import app
from app.repositories.notifications import generate_budget_alerts

USER_ID = uuid4()
CATEGORY_ID = uuid4()
BUDGET_ID = uuid4()
PLAN_ID = uuid4()


class FakePool:
    """Minimal asyncpg.Pool stand-in for generate_budget_alerts' own SQL -
    routes each call by a distinguishing substring of the query text (the
    same approach this repo's other repository-level tests use a mocked
    `pool.fetchval`/`pool.fetch` for), rather than standing up a real
    Postgres connection.
    """

    def __init__(self, *, category, near_threshold, budgets, consumed_after_sequence):
        self.category = category
        self.near_threshold = near_threshold
        self.budgets = budgets
        self.consumed_after_sequence = list(consumed_after_sequence)
        self.executed = []
        self.fetch_called = False

    async def fetchrow(self, query, *args):
        assert "FROM categories" in query
        return self.category

    async def fetchval(self, query, *args):
        if "near_threshold" in query:
            return self.near_threshold
        assert "FROM allocations" in query
        return self.consumed_after_sequence.pop(0)

    async def fetch(self, query, *args):
        self.fetch_called = True
        assert "FROM budgets" in query
        return self.budgets

    async def execute(self, query, *args):
        self.executed.append((query, args))


def make_budget(*, limit_amount=Decimal("1000.00")):
    return {
        "budget_id": BUDGET_ID,
        "plan_id": PLAN_ID,
        "limit_amount": limit_amount,
        "starts_at": date(2026, 10, 1),
        "ends_at": date(2026, 10, 7),
        "plan_name": "This week",
        "category_name": "Foodstuff",
    }


async def _run_generate(pool):
    await generate_budget_alerts(
        pool,
        user_id=USER_ID,
        category_id=CATEGORY_ID,
        txn_date=date(2026, 10, 3),
        currency="KES",
        new_amount=Decimal("850.00"),
    )


async def test_crossing_from_ok_to_near_writes_exactly_one_notification():
    pool = FakePool(
        category={"type": "expense"},
        near_threshold=Decimal("0.80"),
        budgets=[make_budget()],
        # before = 850 (after) - 850 (new_amount) = 0 -> ok (0%)
        # after = 850 / 1000 = 85% -> near
        consumed_after_sequence=[Decimal("850.00")],
    )

    await _run_generate(pool)

    assert len(pool.executed) == 1
    _, args = pool.executed[0]
    # (user_id, state, title, body, budget_id, plan_id)
    assert args[0] == USER_ID
    assert args[1] == "near"
    assert args[4] == BUDGET_ID
    assert args[5] == PLAN_ID
    assert "Foodstuff" in args[3]


async def test_staying_in_the_same_state_writes_no_notification():
    pool = FakePool(
        category={"type": "expense"},
        near_threshold=Decimal("0.80"),
        budgets=[make_budget()],
        # before = 400 - 100 = 300 -> 30% -> ok
        # after = 400 / 1000 = 40% -> still ok
        consumed_after_sequence=[Decimal("400.00")],
    )

    await generate_budget_alerts(
        pool,
        user_id=USER_ID,
        category_id=CATEGORY_ID,
        txn_date=date(2026, 10, 3),
        currency="KES",
        new_amount=Decimal("100.00"),
    )

    assert pool.executed == []


async def test_income_category_writes_no_notification_and_never_queries_budgets():
    pool = FakePool(
        category={"type": "income"},
        near_threshold=Decimal("0.80"),
        budgets=[make_budget()],
        consumed_after_sequence=[],
    )

    await _run_generate(pool)

    assert pool.executed == []
    assert pool.fetch_called is False


async def test_uncapped_budget_writes_no_notification():
    pool = FakePool(
        category={"type": "expense"},
        near_threshold=Decimal("0.80"),
        budgets=[make_budget(limit_amount=None)],
        consumed_after_sequence=[],
    )

    await _run_generate(pool)

    assert pool.executed == []


async def test_no_matching_budget_writes_no_notification():
    pool = FakePool(
        category={"type": "expense"},
        near_threshold=Decimal("0.80"),
        budgets=[],
        consumed_after_sequence=[],
    )

    await _run_generate(pool)

    assert pool.executed == []


# ---------------------------------------------------------------------
# Endpoint-level wiring - confirms the two reconciliation endpoints call
# generate_budget_alerts for a categorized split, and never for a
# transfer-shaped or null-category one.
# ---------------------------------------------------------------------

TRANSACTION_ID = uuid4()
EXPENSE_CATEGORY_ID = uuid4()

TRANSACTION_ROW = {
    "id": str(TRANSACTION_ID),
    "account_id": str(uuid4()),
    "txn_date": "2026-09-01",
    "amount": 1000.0,
    "currency": "KES",
    "direction": "out",
    "counterparty": "Naivas Supermarket",
    "description": None,
    "balance_after": 29000.0,
    "status": "unreconciled",
    "import_id": None,
    "sub_ledger_id": None,
    "sub_ledger_name": None,
}


@pytest.fixture(scope="module")
def client():
    with TestClient(app) as c:
        yield c


@pytest.fixture
def fake_pool():
    app.dependency_overrides[get_pool] = lambda: object()
    yield
    app.dependency_overrides.pop(get_pool, None)


@pytest.fixture
def fake_user():
    user_id = uuid4()
    app.dependency_overrides[get_current_user_id] = lambda: user_id
    yield user_id
    app.dependency_overrides.pop(get_current_user_id, None)


def test_create_allocations_triggers_alert_generation_for_a_categorized_split(
    client, fake_user, fake_pool, monkeypatch
):
    monkeypatch.setattr("app.api.transactions.get_transaction", AsyncMock(return_value=TRANSACTION_ROW))
    monkeypatch.setattr("app.api.transactions.get_allocations", AsyncMock(return_value=[]))
    monkeypatch.setattr(
        "app.api.transactions.get_categories_by_ids",
        AsyncMock(return_value=[{"id": EXPENSE_CATEGORY_ID, "name": "Transport", "type": "expense"}]),
    )
    monkeypatch.setattr("app.api.transactions.insert_allocations", AsyncMock(return_value=None))
    mock_generate = AsyncMock(return_value=None)
    monkeypatch.setattr("app.api.transactions.generate_budget_alerts", mock_generate)

    response = client.post(
        f"/transactions/{TRANSACTION_ID}/allocations",
        json={
            "allocations": [
                {"category_id": str(EXPENSE_CATEGORY_ID), "amount": 1000.0, "note": None},
            ]
        },
    )

    assert response.status_code == 201
    mock_generate.assert_awaited_once()
    _, kwargs = mock_generate.call_args
    assert kwargs["category_id"] == EXPENSE_CATEGORY_ID
    assert kwargs["currency"] == "KES"
    assert kwargs["new_amount"] == Decimal("1000.0")


def test_create_allocations_with_a_transfer_shaped_split_never_triggers_alert_generation(
    client, fake_user, fake_pool, monkeypatch
):
    transfer_reason_id = uuid4()
    monkeypatch.setattr("app.api.transactions.get_transaction", AsyncMock(return_value=TRANSACTION_ROW))
    monkeypatch.setattr("app.api.transactions.get_allocations", AsyncMock(return_value=[]))
    monkeypatch.setattr(
        "app.api.transactions.get_transfer_reasons_by_ids",
        AsyncMock(return_value=[{"id": transfer_reason_id, "name": "Loan Repayment"}]),
    )
    monkeypatch.setattr("app.api.transactions.insert_allocations", AsyncMock(return_value=None))
    mock_generate = AsyncMock(return_value=None)
    monkeypatch.setattr("app.api.transactions.generate_budget_alerts", mock_generate)

    response = client.post(
        f"/transactions/{TRANSACTION_ID}/allocations",
        json={
            "allocations": [
                {"transfer_reason_id": str(transfer_reason_id), "amount": 1000.0, "note": None},
            ]
        },
    )

    assert response.status_code == 201
    mock_generate.assert_not_awaited()


def test_create_allocations_with_a_null_category_split_never_triggers_alert_generation(
    client, fake_user, fake_pool, monkeypatch
):
    monkeypatch.setattr("app.api.transactions.get_transaction", AsyncMock(return_value=TRANSACTION_ROW))
    monkeypatch.setattr("app.api.transactions.get_allocations", AsyncMock(return_value=[]))
    monkeypatch.setattr("app.api.transactions.insert_allocations", AsyncMock(return_value=None))
    mock_generate = AsyncMock(return_value=None)
    monkeypatch.setattr("app.api.transactions.generate_budget_alerts", mock_generate)

    response = client.post(
        f"/transactions/{TRANSACTION_ID}/allocations",
        json={"allocations": [{"category_id": None, "amount": 1000.0, "note": None}]},
    )

    assert response.status_code == 201
    mock_generate.assert_not_awaited()


def test_bulk_allocate_triggers_alert_generation_grouped_by_category_and_date(
    client, fake_user, fake_pool, monkeypatch
):
    t1 = uuid4()
    monkeypatch.setattr(
        "app.api.transactions.get_transactions_with_allocated",
        AsyncMock(
            return_value=[
                {
                    "id": t1,
                    "txn_date": date(2026, 9, 1),
                    "amount": 1000.0,
                    "currency": "KES",
                    "direction": "out",
                    "allocated": 0.0,
                }
            ]
        ),
    )
    monkeypatch.setattr(
        "app.api.transactions.get_categories_by_ids",
        AsyncMock(return_value=[{"id": EXPENSE_CATEGORY_ID, "name": "Transport", "type": "expense"}]),
    )
    monkeypatch.setattr("app.api.transactions.insert_bulk_allocations", AsyncMock(return_value=None))
    mock_generate = AsyncMock(return_value=None)
    monkeypatch.setattr("app.api.transactions.generate_budget_alerts", mock_generate)

    response = client.post(
        "/transactions/bulk-allocate",
        json={"transaction_ids": [str(t1)], "category_id": str(EXPENSE_CATEGORY_ID)},
    )

    assert response.status_code == 201
    mock_generate.assert_awaited_once()
    _, kwargs = mock_generate.call_args
    assert kwargs["category_id"] == EXPENSE_CATEGORY_ID
    assert kwargs["txn_date"] == date(2026, 9, 1)
    assert kwargs["currency"] == "KES"
    assert kwargs["new_amount"] == Decimal("1000.0")


def test_bulk_allocate_with_a_null_category_never_triggers_alert_generation(
    client, fake_user, fake_pool, monkeypatch
):
    t1 = uuid4()
    monkeypatch.setattr(
        "app.api.transactions.get_transactions_with_allocated",
        AsyncMock(
            return_value=[
                {
                    "id": t1,
                    "txn_date": date(2026, 9, 1),
                    "amount": 1000.0,
                    "currency": "KES",
                    "direction": "out",
                    "allocated": 0.0,
                }
            ]
        ),
    )
    monkeypatch.setattr("app.api.transactions.insert_bulk_allocations", AsyncMock(return_value=None))
    mock_generate = AsyncMock(return_value=None)
    monkeypatch.setattr("app.api.transactions.generate_budget_alerts", mock_generate)

    response = client.post(
        "/transactions/bulk-allocate",
        json={"transaction_ids": [str(t1)], "category_id": None},
    )

    assert response.status_code == 201
    mock_generate.assert_not_awaited()
