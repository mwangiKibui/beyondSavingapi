import logging
from unittest.mock import AsyncMock
from uuid import uuid4

import pytest
from fastapi.testclient import TestClient

from app.core.db import get_pool
from app.core.security import get_current_user_id
from app.main import app

TRANSACTION_ID = uuid4()
ALLOCATION_ID = uuid4()
OTHER_ALLOCATION_ID = uuid4()
EXPENSE_CATEGORY_ID = uuid4()
OTHER_EXPENSE_CATEGORY_ID = uuid4()
INCOME_CATEGORY_ID = uuid4()
TRANSFER_REASON_ID = uuid4()

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
    "status": "reconciled",
    "import_id": None,
    "sub_ledger_id": None,
    "sub_ledger_name": None,
}


def allocation_row(allocation_id=ALLOCATION_ID, category_id=EXPENSE_CATEGORY_ID, amount=600.0, **overrides):
    row = {
        "id": allocation_id,
        "category_id": category_id,
        "category_name": "Groceries" if category_id else None,
        "transfer_reason_id": None,
        "transfer_reason_name": None,
        "source_account_id": None,
        "source_account_name": None,
        "source_sub_ledger_id": None,
        "source_sub_ledger_name": None,
        "source_description": None,
        "amount": amount,
        "currency": "KES",
        "original_amount": amount,
        "original_currency": "KES",
        "note": None,
        "created_at": "2026-09-01T12:00:00Z",
    }
    row.update(overrides)
    return row


EXISTING_ALLOCATIONS = [
    allocation_row(),
    allocation_row(allocation_id=OTHER_ALLOCATION_ID, category_id=None, amount=400.0),
]

ALLOCATION_URL = f"/transactions/{TRANSACTION_ID}/allocations/{ALLOCATION_ID}"
ALLOCATIONS_URL = f"/transactions/{TRANSACTION_ID}/allocations"


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


@pytest.fixture
def owned_transaction(monkeypatch):
    monkeypatch.setattr("app.api.transactions.get_transaction", AsyncMock(return_value=TRANSACTION_ROW))
    monkeypatch.setattr("app.api.transactions.get_allocations", AsyncMock(return_value=EXISTING_ALLOCATIONS))
    monkeypatch.setattr("app.api.transactions.generate_budget_alerts", AsyncMock(return_value=None))


def expense_categories(*ids):
    return AsyncMock(return_value=[{"id": category_id, "name": "Expense", "type": "expense"} for category_id in ids])


# --- PATCH /transactions/{id}/allocations/{allocation_id} ---


def test_update_allocation_requires_auth(client):
    response = client.patch(ALLOCATION_URL, json={"note": "x"})
    assert response.status_code == 401


def test_update_allocation_returns_503_when_db_unreachable(client, fake_user):
    response = client.patch(ALLOCATION_URL, json={"note": "x"})
    assert response.status_code == 503


def test_update_allocation_returns_404_when_transaction_not_found(client, fake_user, fake_pool, monkeypatch):
    monkeypatch.setattr("app.api.transactions.get_transaction", AsyncMock(return_value=None))

    response = client.patch(ALLOCATION_URL, json={"note": "x"})

    assert response.status_code == 404
    assert response.json()["detail"] == "Transaction not found"


def test_update_allocation_returns_404_when_allocation_not_on_this_transaction(
    client, fake_user, fake_pool, owned_transaction
):
    response = client.patch(f"{ALLOCATIONS_URL}/{uuid4()}", json={"note": "x"})

    assert response.status_code == 404
    assert response.json()["detail"] == "Allocation not found"


def test_update_allocation_rejects_a_non_positive_amount(client, fake_user, fake_pool, owned_transaction):
    response = client.patch(ALLOCATION_URL, json={"amount": 0})

    assert response.status_code == 422
    assert "greater than zero" in response.json()["detail"]


def test_update_allocation_rejects_an_amount_exceeding_what_the_other_splits_leave(
    client, fake_user, fake_pool, owned_transaction
):
    # The other split already holds 400 of the 1000 - this one can be at most 600.
    response = client.patch(ALLOCATION_URL, json={"amount": 700.0})

    assert response.status_code == 422
    assert "can't exceed the remaining 600.00 KES" in response.json()["detail"]


def test_update_allocation_excludes_its_own_current_amount_from_the_over_allocation_check(
    client, fake_user, fake_pool, owned_transaction, monkeypatch
):
    # Counting this split's existing 600 as well would wrongly cap it at 0.
    mock_update = AsyncMock(return_value=None)
    monkeypatch.setattr("app.api.transactions.update_allocation", mock_update)

    response = client.patch(ALLOCATION_URL, json={"amount": 600.0})

    assert response.status_code == 200
    mock_update.assert_awaited_once()


def test_update_allocation_returns_404_when_the_category_is_not_found(
    client, fake_user, fake_pool, owned_transaction, monkeypatch
):
    monkeypatch.setattr("app.api.transactions.get_categories_by_ids", AsyncMock(return_value=[]))

    response = client.patch(ALLOCATION_URL, json={"category_id": str(OTHER_EXPENSE_CATEGORY_ID)})

    assert response.status_code == 404
    assert response.json()["detail"] == "Category not found"


def test_update_allocation_rejects_a_category_type_that_does_not_match_direction(
    client, fake_user, fake_pool, owned_transaction, monkeypatch
):
    monkeypatch.setattr(
        "app.api.transactions.get_categories_by_ids",
        AsyncMock(return_value=[{"id": INCOME_CATEGORY_ID, "name": "Salary", "type": "income"}]),
    )

    response = client.patch(ALLOCATION_URL, json={"category_id": str(INCOME_CATEGORY_ID)})

    assert response.status_code == 422
    assert "doesn't match this transaction's direction" in response.json()["detail"]


def test_update_allocation_rejects_a_category_on_a_transfer_shaped_split(
    client, fake_user, fake_pool, monkeypatch
):
    transfer_split = allocation_row(
        category_id=None, amount=1000.0, transfer_reason_id=TRANSFER_REASON_ID, transfer_reason_name="Savings"
    )
    monkeypatch.setattr("app.api.transactions.get_transaction", AsyncMock(return_value=TRANSACTION_ROW))
    monkeypatch.setattr("app.api.transactions.get_allocations", AsyncMock(return_value=[transfer_split]))
    monkeypatch.setattr("app.api.transactions.get_categories_by_ids", expense_categories(EXPENSE_CATEGORY_ID))

    response = client.patch(ALLOCATION_URL, json={"category_id": str(EXPENSE_CATEGORY_ID)})

    assert response.status_code == 422
    assert "Can't set both a category and a transfer reason" in response.json()["detail"]


def test_update_allocation_merges_only_the_fields_sent(
    client, fake_user, fake_pool, owned_transaction, monkeypatch
):
    mock_update = AsyncMock(return_value=None)
    monkeypatch.setattr("app.api.transactions.update_allocation", mock_update)

    response = client.patch(ALLOCATION_URL, json={"note": "Weekly shop"})

    assert response.status_code == 200
    _, kwargs = mock_update.call_args
    assert kwargs["allocation_id"] == ALLOCATION_ID
    assert kwargs["transaction_id"] == TRANSACTION_ID
    assert kwargs["category_id"] == EXPENSE_CATEGORY_ID
    assert kwargs["amount"] == 600.0
    assert kwargs["note"] == "Weekly shop"


def test_update_allocation_can_switch_a_split_to_no_category(
    client, fake_user, fake_pool, owned_transaction, monkeypatch
):
    mock_update = AsyncMock(return_value=None)
    monkeypatch.setattr("app.api.transactions.update_allocation", mock_update)

    response = client.patch(ALLOCATION_URL, json={"category_id": None})

    assert response.status_code == 200
    _, kwargs = mock_update.call_args
    assert kwargs["category_id"] is None


def test_update_allocation_raises_budget_alerts_for_an_increase_on_the_same_category(
    client, fake_user, fake_pool, monkeypatch
):
    smaller_split = allocation_row(amount=300.0)
    monkeypatch.setattr("app.api.transactions.get_transaction", AsyncMock(return_value=TRANSACTION_ROW))
    monkeypatch.setattr("app.api.transactions.get_allocations", AsyncMock(return_value=[smaller_split]))
    monkeypatch.setattr("app.api.transactions.update_allocation", AsyncMock(return_value=None))
    mock_alerts = AsyncMock(return_value=None)
    monkeypatch.setattr("app.api.transactions.generate_budget_alerts", mock_alerts)

    response = client.patch(ALLOCATION_URL, json={"amount": 500.0})

    assert response.status_code == 200
    _, kwargs = mock_alerts.call_args
    assert kwargs["category_id"] == EXPENSE_CATEGORY_ID
    assert float(kwargs["new_amount"]) == 200.0


def test_update_allocation_raises_budget_alerts_for_the_full_amount_on_a_new_category(
    client, fake_user, fake_pool, owned_transaction, monkeypatch
):
    monkeypatch.setattr(
        "app.api.transactions.get_categories_by_ids", expense_categories(OTHER_EXPENSE_CATEGORY_ID)
    )
    monkeypatch.setattr("app.api.transactions.update_allocation", AsyncMock(return_value=None))
    mock_alerts = AsyncMock(return_value=None)
    monkeypatch.setattr("app.api.transactions.generate_budget_alerts", mock_alerts)

    response = client.patch(ALLOCATION_URL, json={"category_id": str(OTHER_EXPENSE_CATEGORY_ID)})

    assert response.status_code == 200
    _, kwargs = mock_alerts.call_args
    assert kwargs["category_id"] == OTHER_EXPENSE_CATEGORY_ID
    assert float(kwargs["new_amount"]) == 600.0


def test_update_allocation_raises_no_budget_alert_for_a_decrease(
    client, fake_user, fake_pool, owned_transaction, monkeypatch
):
    monkeypatch.setattr("app.api.transactions.update_allocation", AsyncMock(return_value=None))
    mock_alerts = AsyncMock(return_value=None)
    monkeypatch.setattr("app.api.transactions.generate_budget_alerts", mock_alerts)

    response = client.patch(ALLOCATION_URL, json={"amount": 100.0})

    assert response.status_code == 200
    mock_alerts.assert_not_awaited()


def test_update_allocation_still_succeeds_when_budget_alerts_fail(
    client, fake_user, fake_pool, owned_transaction, monkeypatch
):
    monkeypatch.setattr(
        "app.api.transactions.get_categories_by_ids", expense_categories(OTHER_EXPENSE_CATEGORY_ID)
    )
    monkeypatch.setattr("app.api.transactions.update_allocation", AsyncMock(return_value=None))
    monkeypatch.setattr(
        "app.api.transactions.generate_budget_alerts", AsyncMock(side_effect=RuntimeError("alerts down"))
    )

    response = client.patch(ALLOCATION_URL, json={"category_id": str(OTHER_EXPENSE_CATEGORY_ID)})

    assert response.status_code == 200


def test_update_allocation_returns_the_refreshed_transaction_and_allocations(
    client, fake_user, fake_pool, monkeypatch
):
    updated = allocation_row(amount=500.0)
    monkeypatch.setattr(
        "app.api.transactions.get_transaction",
        AsyncMock(side_effect=[TRANSACTION_ROW, {**TRANSACTION_ROW, "status": "partial"}]),
    )
    monkeypatch.setattr(
        "app.api.transactions.get_allocations",
        AsyncMock(side_effect=[EXISTING_ALLOCATIONS, [updated, EXISTING_ALLOCATIONS[1]]]),
    )
    monkeypatch.setattr("app.api.transactions.update_allocation", AsyncMock(return_value=None))
    monkeypatch.setattr("app.api.transactions.generate_budget_alerts", AsyncMock(return_value=None))

    response = client.patch(ALLOCATION_URL, json={"amount": 500.0})

    assert response.status_code == 200
    body = response.json()
    assert body["status"] == "partial"
    assert [allocation["original_amount"] for allocation in body["allocations"]] == [500.0, 400.0]


def test_update_allocation_returns_generic_error_and_logs_on_unexpected_failure(
    client, fake_user, fake_pool, owned_transaction, monkeypatch, caplog
):
    monkeypatch.setattr(
        "app.api.transactions.update_allocation", AsyncMock(side_effect=RuntimeError("connection reset"))
    )

    with caplog.at_level(logging.ERROR):
        response = client.patch(ALLOCATION_URL, json={"note": "x"})

    assert response.status_code == 500
    assert response.json()["detail"] == "Something went wrong. Please try again."
    assert "connection reset" not in response.text
    assert "connection reset" in caplog.text


# --- DELETE /transactions/{id}/allocations/{allocation_id} ---


def test_delete_allocation_requires_auth(client):
    response = client.delete(ALLOCATION_URL)
    assert response.status_code == 401


def test_delete_allocation_returns_503_when_db_unreachable(client, fake_user):
    response = client.delete(ALLOCATION_URL)
    assert response.status_code == 503


def test_delete_allocation_returns_404_when_transaction_not_found(client, fake_user, fake_pool, monkeypatch):
    monkeypatch.setattr("app.api.transactions.get_transaction", AsyncMock(return_value=None))

    response = client.delete(ALLOCATION_URL)

    assert response.status_code == 404
    assert response.json()["detail"] == "Transaction not found"


def test_delete_allocation_returns_404_when_allocation_not_on_this_transaction(
    client, fake_user, fake_pool, owned_transaction, monkeypatch
):
    monkeypatch.setattr("app.api.transactions.delete_allocation", AsyncMock(return_value=False))

    response = client.delete(f"{ALLOCATIONS_URL}/{uuid4()}")

    assert response.status_code == 404
    assert response.json()["detail"] == "Allocation not found"


def test_delete_allocation_removes_the_split_and_returns_the_refreshed_detail(
    client, fake_user, fake_pool, monkeypatch
):
    monkeypatch.setattr(
        "app.api.transactions.get_transaction",
        AsyncMock(side_effect=[TRANSACTION_ROW, {**TRANSACTION_ROW, "status": "partial"}]),
    )
    monkeypatch.setattr("app.api.transactions.get_allocations", AsyncMock(return_value=[EXISTING_ALLOCATIONS[1]]))
    mock_delete = AsyncMock(return_value=True)
    monkeypatch.setattr("app.api.transactions.delete_allocation", mock_delete)

    response = client.delete(ALLOCATION_URL)

    assert response.status_code == 200
    _, kwargs = mock_delete.call_args
    assert kwargs["allocation_id"] == ALLOCATION_ID
    assert kwargs["transaction_id"] == TRANSACTION_ID
    body = response.json()
    assert body["status"] == "partial"
    assert len(body["allocations"]) == 1


def test_delete_allocation_returns_generic_error_and_logs_on_unexpected_failure(
    client, fake_user, fake_pool, owned_transaction, monkeypatch, caplog
):
    monkeypatch.setattr(
        "app.api.transactions.delete_allocation", AsyncMock(side_effect=RuntimeError("connection reset"))
    )

    with caplog.at_level(logging.ERROR):
        response = client.delete(ALLOCATION_URL)

    assert response.status_code == 500
    assert response.json()["detail"] == "Something went wrong. Please try again."
    assert "connection reset" in caplog.text


# --- DELETE /transactions/{id}/allocations (clear all) ---


def test_clear_allocations_requires_auth(client):
    response = client.delete(ALLOCATIONS_URL)
    assert response.status_code == 401


def test_clear_allocations_returns_503_when_db_unreachable(client, fake_user):
    response = client.delete(ALLOCATIONS_URL)
    assert response.status_code == 503


def test_clear_allocations_returns_404_when_transaction_not_found(client, fake_user, fake_pool, monkeypatch):
    monkeypatch.setattr("app.api.transactions.get_transaction", AsyncMock(return_value=None))

    response = client.delete(ALLOCATIONS_URL)

    assert response.status_code == 404


def test_clear_allocations_removes_every_split_and_returns_an_unreconciled_transaction(
    client, fake_user, fake_pool, monkeypatch
):
    monkeypatch.setattr(
        "app.api.transactions.get_transaction",
        AsyncMock(side_effect=[TRANSACTION_ROW, {**TRANSACTION_ROW, "status": "unreconciled"}]),
    )
    monkeypatch.setattr("app.api.transactions.get_allocations", AsyncMock(return_value=[]))
    mock_clear = AsyncMock(return_value=2)
    monkeypatch.setattr("app.api.transactions.delete_allocations_for_transaction", mock_clear)

    response = client.delete(ALLOCATIONS_URL)

    assert response.status_code == 200
    _, kwargs = mock_clear.call_args
    assert kwargs["transaction_id"] == TRANSACTION_ID
    body = response.json()
    assert body["status"] == "unreconciled"
    assert body["allocations"] == []


def test_clear_allocations_returns_generic_error_and_logs_on_unexpected_failure(
    client, fake_user, fake_pool, owned_transaction, monkeypatch, caplog
):
    monkeypatch.setattr(
        "app.api.transactions.delete_allocations_for_transaction",
        AsyncMock(side_effect=RuntimeError("connection reset")),
    )

    with caplog.at_level(logging.ERROR):
        response = client.delete(ALLOCATIONS_URL)

    assert response.status_code == 500
    assert "connection reset" in caplog.text
