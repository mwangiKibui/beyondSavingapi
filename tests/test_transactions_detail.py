import logging
from unittest.mock import AsyncMock
from uuid import uuid4

import pytest
from fastapi.testclient import TestClient

from app.core.db import get_pool
from app.core.security import get_current_user_id
from app.main import app

TRANSACTION_ID = uuid4()

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

# Spread into an allocation dict fixture below a categorized/no-category
# split - transfer-shaped fields (ab-134) are all None for one of those,
# and get_allocations' real query now always returns these keys.
NO_TRANSFER_FIELDS = {
    "transfer_reason_id": None,
    "transfer_reason_name": None,
    "source_account_id": None,
    "source_account_name": None,
    "source_sub_ledger_id": None,
    "source_sub_ledger_name": None,
    "source_description": None,
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


def test_get_transaction_requires_auth(client):
    response = client.get(f"/transactions/{TRANSACTION_ID}")
    assert response.status_code == 401


def test_get_transaction_returns_503_when_db_unreachable(client, fake_user):
    response = client.get(f"/transactions/{TRANSACTION_ID}")
    assert response.status_code == 503


def test_get_transaction_returns_404_when_not_found(client, fake_user, fake_pool, monkeypatch):
    monkeypatch.setattr("app.api.transactions.get_transaction", AsyncMock(return_value=None))

    response = client.get(f"/transactions/{TRANSACTION_ID}")

    assert response.status_code == 404


def test_get_transaction_with_no_allocations(client, fake_user, fake_pool, monkeypatch):
    monkeypatch.setattr("app.api.transactions.get_transaction", AsyncMock(return_value={**TRANSACTION_ROW, "status": "unreconciled"}))
    monkeypatch.setattr("app.api.transactions.get_allocations", AsyncMock(return_value=[]))

    response = client.get(f"/transactions/{TRANSACTION_ID}")

    assert response.status_code == 200
    body = response.json()
    assert body["status"] == "unreconciled"
    assert body["allocations"] == []


def test_get_transaction_with_a_categorized_allocation(client, fake_user, fake_pool, monkeypatch):
    category_id = uuid4()
    allocation = {
        "id": str(uuid4()),
        "category_id": str(category_id),
        "category_name": "Groceries",
        **NO_TRANSFER_FIELDS,
        "amount": 1000.0,
        "currency": "KES",
        "original_amount": 1000.0,
        "original_currency": "KES",
        "note": None,
        "created_at": "2026-09-01T12:00:00Z",
    }
    monkeypatch.setattr("app.api.transactions.get_transaction", AsyncMock(return_value=TRANSACTION_ROW))
    monkeypatch.setattr("app.api.transactions.get_allocations", AsyncMock(return_value=[allocation]))

    response = client.get(f"/transactions/{TRANSACTION_ID}")

    assert response.status_code == 200
    assert response.json()["allocations"] == [allocation]


def test_get_transaction_with_a_null_category_allocation(client, fake_user, fake_pool, monkeypatch):
    # ab-124: reconciled with no category is a real, deliberate allocation
    # state - category_id/category_name both null, but still reconciled.
    allocation = {
        "id": str(uuid4()),
        "category_id": None,
        "category_name": None,
        **NO_TRANSFER_FIELDS,
        "amount": 1000.0,
        "currency": "KES",
        "original_amount": 1000.0,
        "original_currency": "KES",
        "note": "Sent to Sacco for loan repayment",
        "created_at": "2026-09-01T12:00:00Z",
    }
    monkeypatch.setattr("app.api.transactions.get_transaction", AsyncMock(return_value=TRANSACTION_ROW))
    monkeypatch.setattr("app.api.transactions.get_allocations", AsyncMock(return_value=[allocation]))

    response = client.get(f"/transactions/{TRANSACTION_ID}")

    assert response.status_code == 200
    body = response.json()
    assert body["status"] == "reconciled"
    assert body["allocations"] == [allocation]


def test_get_transaction_allocation_keeps_original_amount_distinct_from_converted(
    client, fake_user, fake_pool, monkeypatch
):
    # A KES transaction allocated onto a USD budget plan: `amount`/`currency`
    # is the converted, plan-currency figure, while `original_amount`/
    # `original_currency` is the transaction's own currency - the one
    # reconciliation-complete is actually judged against (see
    # product-brief.html's "Reconciliation vs. budget math use different
    # amounts" decision), and what a reconciliation-history table should
    # display instead of the converted figure.
    allocation = {
        "id": str(uuid4()),
        "category_id": str(uuid4()),
        "category_name": "Travel",
        "amount": 7.69,
        "currency": "USD",
        "original_amount": 1000.0,
        "original_currency": "KES",
        "note": None,
        "created_at": "2026-09-01T12:00:00Z",
    }
    monkeypatch.setattr("app.api.transactions.get_transaction", AsyncMock(return_value=TRANSACTION_ROW))
    monkeypatch.setattr("app.api.transactions.get_allocations", AsyncMock(return_value=[allocation]))

    response = client.get(f"/transactions/{TRANSACTION_ID}")

    assert response.status_code == 200
    body = response.json()["allocations"][0]
    assert (body["amount"], body["currency"]) == (7.69, "USD")
    assert (body["original_amount"], body["original_currency"]) == (1000.0, "KES")


def test_get_transaction_with_a_hybrid_split(client, fake_user, fake_pool, monkeypatch):
    categorized = {
        "id": str(uuid4()),
        "category_id": str(uuid4()),
        "category_name": "Transport",
        **NO_TRANSFER_FIELDS,
        "amount": 600.0,
        "currency": "KES",
        "original_amount": 600.0,
        "original_currency": "KES",
        "note": None,
        "created_at": "2026-09-01T12:00:00Z",
    }
    no_category = {
        "id": str(uuid4()),
        "category_id": None,
        "category_name": None,
        **NO_TRANSFER_FIELDS,
        "amount": 400.0,
        "currency": "KES",
        "original_amount": 400.0,
        "original_currency": "KES",
        "note": "Cash gift to mom",
        "created_at": "2026-09-01T12:05:00Z",
    }
    monkeypatch.setattr("app.api.transactions.get_transaction", AsyncMock(return_value=TRANSACTION_ROW))
    monkeypatch.setattr("app.api.transactions.get_allocations", AsyncMock(return_value=[categorized, no_category]))

    response = client.get(f"/transactions/{TRANSACTION_ID}")

    assert response.status_code == 200
    assert response.json()["allocations"] == [categorized, no_category]


def test_get_transaction_with_a_transfer_shaped_allocation(client, fake_user, fake_pool, monkeypatch):
    # ab-134: a transfer-shaped split has category_id/category_name null
    # and transfer_reason_id/name set instead, plus a resolved source name
    # (here, a tracked sub-ledger) - the frontend gets everything it needs
    # to render this without a second round-trip.
    allocation = {
        "id": str(uuid4()),
        "category_id": None,
        "category_name": None,
        "transfer_reason_id": str(uuid4()),
        "transfer_reason_name": "Loan Repayment",
        "source_account_id": None,
        "source_account_name": None,
        "source_sub_ledger_id": str(uuid4()),
        "source_sub_ledger_name": "Ordinary Deposit",
        "source_description": None,
        "amount": 1000.0,
        "currency": "KES",
        "original_amount": 1000.0,
        "original_currency": "KES",
        "note": None,
        "created_at": "2026-09-01T12:00:00Z",
    }
    monkeypatch.setattr("app.api.transactions.get_transaction", AsyncMock(return_value=TRANSACTION_ROW))
    monkeypatch.setattr("app.api.transactions.get_allocations", AsyncMock(return_value=[allocation]))

    response = client.get(f"/transactions/{TRANSACTION_ID}")

    assert response.status_code == 200
    assert response.json()["allocations"] == [allocation]


def test_get_transaction_passes_ids_to_the_repository(client, fake_user, fake_pool, monkeypatch):
    mock_get_transaction = AsyncMock(return_value=TRANSACTION_ROW)
    mock_get_allocations = AsyncMock(return_value=[])
    monkeypatch.setattr("app.api.transactions.get_transaction", mock_get_transaction)
    monkeypatch.setattr("app.api.transactions.get_allocations", mock_get_allocations)

    client.get(f"/transactions/{TRANSACTION_ID}")

    _, kwargs = mock_get_transaction.call_args
    assert kwargs["transaction_id"] == TRANSACTION_ID
    assert kwargs["user_id"] == fake_user
    _, kwargs = mock_get_allocations.call_args
    assert kwargs["transaction_id"] == TRANSACTION_ID


def test_get_transaction_returns_generic_error_and_logs_on_unexpected_failure(client, fake_user, fake_pool, monkeypatch, caplog):
    monkeypatch.setattr(
        "app.api.transactions.get_transaction", AsyncMock(side_effect=RuntimeError("connection reset"))
    )

    with caplog.at_level(logging.ERROR):
        response = client.get(f"/transactions/{TRANSACTION_ID}")

    assert response.status_code == 500
    assert response.json()["detail"] == "Something went wrong. Please try again."
    assert "connection reset" not in response.text
    assert "connection reset" in caplog.text
