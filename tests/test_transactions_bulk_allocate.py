import logging
from unittest.mock import AsyncMock
from uuid import uuid4

import pytest
from fastapi.testclient import TestClient

from app.core.db import get_pool
from app.core.security import get_current_user_id
from app.main import app

EXPENSE_CATEGORY_ID = uuid4()
INCOME_CATEGORY_ID = uuid4()


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


def transaction_row(id_, *, amount=1000.0, allocated=0.0, currency="KES", direction="out"):
    return {"id": id_, "amount": amount, "currency": currency, "direction": direction, "allocated": allocated}


def test_bulk_allocate_requires_auth(client):
    response = client.post("/transactions/bulk-allocate", json={"transaction_ids": [str(uuid4())]})
    assert response.status_code == 401


def test_bulk_allocate_returns_503_when_db_unreachable(client, fake_user):
    response = client.post("/transactions/bulk-allocate", json={"transaction_ids": [str(uuid4())]})
    assert response.status_code == 503


def test_bulk_allocate_rejects_neither_shape(client, fake_user, fake_pool):
    response = client.post("/transactions/bulk-allocate", json={})
    assert response.status_code == 422
    assert "exactly one" in response.json()["detail"]


def test_bulk_allocate_rejects_both_shapes(client, fake_user, fake_pool):
    response = client.post(
        "/transactions/bulk-allocate",
        json={"transaction_ids": [str(uuid4())], "allocations": [{"transaction_id": str(uuid4())}]},
    )
    assert response.status_code == 422
    assert "exactly one" in response.json()["detail"]


def test_bulk_allocate_rejects_empty_transaction_ids(client, fake_user, fake_pool):
    response = client.post("/transactions/bulk-allocate", json={"transaction_ids": []})
    assert response.status_code == 422


def test_bulk_allocate_rejects_empty_allocations(client, fake_user, fake_pool):
    response = client.post("/transactions/bulk-allocate", json={"allocations": []})
    assert response.status_code == 422


def test_bulk_allocate_returns_404_when_a_transaction_is_not_found(client, fake_user, fake_pool, monkeypatch):
    monkeypatch.setattr("app.api.transactions.get_transactions_with_allocated", AsyncMock(return_value=[]))

    response = client.post("/transactions/bulk-allocate", json={"transaction_ids": [str(uuid4())]})

    assert response.status_code == 404


def test_bulk_allocate_returns_404_when_a_category_is_not_found(client, fake_user, fake_pool, monkeypatch):
    t1 = uuid4()
    monkeypatch.setattr(
        "app.api.transactions.get_transactions_with_allocated",
        AsyncMock(return_value=[transaction_row(t1)]),
    )
    monkeypatch.setattr("app.api.transactions.get_categories_by_ids", AsyncMock(return_value=[]))

    response = client.post(
        "/transactions/bulk-allocate",
        json={"transaction_ids": [str(t1)], "category_id": str(EXPENSE_CATEGORY_ID)},
    )

    assert response.status_code == 404
    assert response.json()["detail"] == "Category not found"


def test_bulk_allocate_rejects_a_category_type_that_does_not_match_a_transaction_direction(
    client, fake_user, fake_pool, monkeypatch
):
    t1 = uuid4()
    monkeypatch.setattr(
        "app.api.transactions.get_transactions_with_allocated",
        AsyncMock(return_value=[transaction_row(t1, direction="out")]),
    )
    monkeypatch.setattr(
        "app.api.transactions.get_categories_by_ids",
        AsyncMock(return_value=[{"id": INCOME_CATEGORY_ID, "name": "Salary", "type": "income"}]),
    )

    response = client.post(
        "/transactions/bulk-allocate",
        json={"transaction_ids": [str(t1)], "category_id": str(INCOME_CATEGORY_ID)},
    )

    assert response.status_code == 422
    assert "doesn't match" in response.json()["detail"]


def test_bulk_allocate_uniform_category_applies_to_every_transaction(client, fake_user, fake_pool, monkeypatch):
    t1, t2 = uuid4(), uuid4()
    monkeypatch.setattr(
        "app.api.transactions.get_transactions_with_allocated",
        AsyncMock(return_value=[transaction_row(t1), transaction_row(t2, amount=500.0)]),
    )
    monkeypatch.setattr(
        "app.api.transactions.get_categories_by_ids",
        AsyncMock(return_value=[{"id": EXPENSE_CATEGORY_ID, "name": "Transport", "type": "expense"}]),
    )
    mock_insert = AsyncMock(return_value=None)
    monkeypatch.setattr("app.api.transactions.insert_bulk_allocations", mock_insert)

    response = client.post(
        "/transactions/bulk-allocate",
        json={"transaction_ids": [str(t1), str(t2)], "category_id": str(EXPENSE_CATEGORY_ID)},
    )

    assert response.status_code == 201
    body = response.json()
    assert sorted(body["updated"]) == sorted([str(t1), str(t2)])
    assert body["skipped"] == []
    _, kwargs = mock_insert.call_args
    assert kwargs["category_ids"] == [EXPENSE_CATEGORY_ID, EXPENSE_CATEGORY_ID]
    assert kwargs["amounts"] == [1000.0, 500.0]


def test_bulk_allocate_uniform_null_category_marks_all_reconciled_no_category(
    client, fake_user, fake_pool, monkeypatch
):
    t1 = uuid4()
    monkeypatch.setattr(
        "app.api.transactions.get_transactions_with_allocated",
        AsyncMock(return_value=[transaction_row(t1)]),
    )
    mock_insert = AsyncMock(return_value=None)
    monkeypatch.setattr("app.api.transactions.insert_bulk_allocations", mock_insert)

    response = client.post("/transactions/bulk-allocate", json={"transaction_ids": [str(t1)], "category_id": None})

    assert response.status_code == 201
    _, kwargs = mock_insert.call_args
    assert kwargs["category_ids"] == [None]


def test_bulk_allocate_per_transaction_mapping_applies_different_categories(
    client, fake_user, fake_pool, monkeypatch
):
    out_txn, in_txn = uuid4(), uuid4()
    monkeypatch.setattr(
        "app.api.transactions.get_transactions_with_allocated",
        AsyncMock(
            return_value=[
                transaction_row(out_txn, direction="out"),
                transaction_row(in_txn, direction="in"),
            ]
        ),
    )
    monkeypatch.setattr(
        "app.api.transactions.get_categories_by_ids",
        AsyncMock(
            return_value=[
                {"id": EXPENSE_CATEGORY_ID, "name": "Transport", "type": "expense"},
                {"id": INCOME_CATEGORY_ID, "name": "Salary", "type": "income"},
            ]
        ),
    )
    mock_insert = AsyncMock(return_value=None)
    monkeypatch.setattr("app.api.transactions.insert_bulk_allocations", mock_insert)

    response = client.post(
        "/transactions/bulk-allocate",
        json={
            "allocations": [
                {"transaction_id": str(out_txn), "category_id": str(EXPENSE_CATEGORY_ID)},
                {"transaction_id": str(in_txn), "category_id": str(INCOME_CATEGORY_ID)},
            ]
        },
    )

    assert response.status_code == 201
    _, kwargs = mock_insert.call_args
    resolved = dict(zip(kwargs["transaction_ids"], kwargs["category_ids"]))
    assert resolved[out_txn] == EXPENSE_CATEGORY_ID
    assert resolved[in_txn] == INCOME_CATEGORY_ID


def test_bulk_allocate_skips_an_already_fully_reconciled_transaction(client, fake_user, fake_pool, monkeypatch):
    reconciled, unreconciled = uuid4(), uuid4()
    monkeypatch.setattr(
        "app.api.transactions.get_transactions_with_allocated",
        AsyncMock(
            return_value=[
                transaction_row(reconciled, amount=1000.0, allocated=1000.0),
                transaction_row(unreconciled, amount=500.0, allocated=0.0),
            ]
        ),
    )
    mock_insert = AsyncMock(return_value=None)
    monkeypatch.setattr("app.api.transactions.insert_bulk_allocations", mock_insert)

    response = client.post(
        "/transactions/bulk-allocate",
        json={"transaction_ids": [str(reconciled), str(unreconciled)], "category_id": None},
    )

    assert response.status_code == 201
    body = response.json()
    assert body["updated"] == [str(unreconciled)]
    assert body["skipped"] == [str(reconciled)]
    _, kwargs = mock_insert.call_args
    assert kwargs["transaction_ids"] == [unreconciled]


def test_bulk_allocate_fills_only_the_remaining_amount_for_a_partially_allocated_transaction(
    client, fake_user, fake_pool, monkeypatch
):
    t1 = uuid4()
    monkeypatch.setattr(
        "app.api.transactions.get_transactions_with_allocated",
        AsyncMock(return_value=[transaction_row(t1, amount=1000.0, allocated=600.0)]),
    )
    mock_insert = AsyncMock(return_value=None)
    monkeypatch.setattr("app.api.transactions.insert_bulk_allocations", mock_insert)

    response = client.post("/transactions/bulk-allocate", json={"transaction_ids": [str(t1)], "category_id": None})

    assert response.status_code == 201
    _, kwargs = mock_insert.call_args
    assert kwargs["amounts"] == [400.0]


def test_bulk_allocate_returns_generic_error_and_logs_on_unexpected_failure(
    client, fake_user, fake_pool, monkeypatch, caplog
):
    monkeypatch.setattr(
        "app.api.transactions.get_transactions_with_allocated",
        AsyncMock(side_effect=RuntimeError("connection reset")),
    )

    with caplog.at_level(logging.ERROR):
        response = client.post("/transactions/bulk-allocate", json={"transaction_ids": [str(uuid4())]})

    assert response.status_code == 500
    assert response.json()["detail"] == "Something went wrong. Please try again."
    assert "connection reset" not in response.text
    assert "connection reset" in caplog.text
