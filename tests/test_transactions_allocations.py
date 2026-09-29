import logging
from unittest.mock import AsyncMock
from uuid import uuid4

import pytest
from fastapi.testclient import TestClient

from app.core.db import get_pool
from app.core.security import get_current_user_id
from app.main import app

TRANSACTION_ID = uuid4()
EXPENSE_CATEGORY_ID = uuid4()
INCOME_CATEGORY_ID = uuid4()

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


def request_body(**overrides):
    body = {"allocations": [{"category_id": None, "amount": 1000.0, "note": None}]}
    body.update(overrides)
    return body


def test_create_allocations_requires_auth(client):
    response = client.post(f"/transactions/{TRANSACTION_ID}/allocations", json=request_body())
    assert response.status_code == 401


def test_create_allocations_returns_503_when_db_unreachable(client, fake_user):
    response = client.post(f"/transactions/{TRANSACTION_ID}/allocations", json=request_body())
    assert response.status_code == 503


def test_create_allocations_returns_404_when_transaction_not_found(client, fake_user, fake_pool, monkeypatch):
    monkeypatch.setattr("app.api.transactions.get_transaction", AsyncMock(return_value=None))

    response = client.post(f"/transactions/{TRANSACTION_ID}/allocations", json=request_body())

    assert response.status_code == 404


def test_create_allocations_rejects_a_non_positive_amount(client, fake_user, fake_pool, monkeypatch):
    monkeypatch.setattr("app.api.transactions.get_transaction", AsyncMock(return_value=TRANSACTION_ROW))

    response = client.post(
        f"/transactions/{TRANSACTION_ID}/allocations",
        json=request_body(allocations=[{"category_id": None, "amount": 0, "note": None}]),
    )

    assert response.status_code == 422
    assert "greater than zero" in response.json()["detail"]


def test_create_allocations_returns_404_when_a_category_is_not_found(client, fake_user, fake_pool, monkeypatch):
    monkeypatch.setattr("app.api.transactions.get_transaction", AsyncMock(return_value=TRANSACTION_ROW))
    monkeypatch.setattr("app.api.transactions.get_categories_by_ids", AsyncMock(return_value=[]))

    response = client.post(
        f"/transactions/{TRANSACTION_ID}/allocations",
        json=request_body(allocations=[{"category_id": str(EXPENSE_CATEGORY_ID), "amount": 1000.0, "note": None}]),
    )

    assert response.status_code == 404
    assert response.json()["detail"] == "Category not found"


def test_create_allocations_rejects_a_category_type_that_does_not_match_direction(
    client, fake_user, fake_pool, monkeypatch
):
    # TRANSACTION_ROW is direction "out" - only an expense category fits.
    monkeypatch.setattr("app.api.transactions.get_transaction", AsyncMock(return_value=TRANSACTION_ROW))
    monkeypatch.setattr(
        "app.api.transactions.get_categories_by_ids",
        AsyncMock(return_value=[{"id": INCOME_CATEGORY_ID, "name": "Salary", "type": "income"}]),
    )

    response = client.post(
        f"/transactions/{TRANSACTION_ID}/allocations",
        json=request_body(allocations=[{"category_id": str(INCOME_CATEGORY_ID), "amount": 1000.0, "note": None}]),
    )

    assert response.status_code == 422
    assert "doesn't match this transaction's direction" in response.json()["detail"]


def test_create_allocations_rejects_over_allocation_against_existing_splits(
    client, fake_user, fake_pool, monkeypatch
):
    monkeypatch.setattr("app.api.transactions.get_transaction", AsyncMock(return_value=TRANSACTION_ROW))
    monkeypatch.setattr(
        "app.api.transactions.get_allocations",
        AsyncMock(
            return_value=[
                {
                    "id": str(uuid4()),
                    "category_id": str(EXPENSE_CATEGORY_ID),
                    "category_name": "Transport",
                    "amount": 600.0,
                    "currency": "KES",
                    "original_amount": 600.0,
                    "original_currency": "KES",
                    "note": None,
                    "created_at": "2026-09-01T12:00:00Z",
                }
            ]
        ),
    )

    response = client.post(
        f"/transactions/{TRANSACTION_ID}/allocations",
        # 600 already allocated + 500 new > 1000 total.
        json=request_body(allocations=[{"category_id": None, "amount": 500.0, "note": None}]),
    )

    assert response.status_code == 422
    assert "can't exceed the remaining" in response.json()["detail"]
    assert "400.00" in response.json()["detail"]


def test_create_allocations_rejects_a_single_split_exceeding_the_full_amount(client, fake_user, fake_pool, monkeypatch):
    monkeypatch.setattr("app.api.transactions.get_transaction", AsyncMock(return_value=TRANSACTION_ROW))
    monkeypatch.setattr("app.api.transactions.get_allocations", AsyncMock(return_value=[]))

    response = client.post(
        f"/transactions/{TRANSACTION_ID}/allocations",
        json=request_body(allocations=[{"category_id": None, "amount": 5000.0, "note": None}]),
    )

    assert response.status_code == 422
    assert "can't exceed the remaining" in response.json()["detail"]


def test_create_allocations_with_a_null_category_split(client, fake_user, fake_pool, monkeypatch):
    monkeypatch.setattr("app.api.transactions.get_transaction", AsyncMock(return_value=TRANSACTION_ROW))
    monkeypatch.setattr("app.api.transactions.get_allocations", AsyncMock(return_value=[]))
    mock_insert = AsyncMock(return_value=None)
    monkeypatch.setattr("app.api.transactions.insert_allocations", mock_insert)

    response = client.post(f"/transactions/{TRANSACTION_ID}/allocations", json=request_body())

    assert response.status_code == 201
    _, kwargs = mock_insert.call_args
    assert kwargs["category_ids"] == [None]
    assert kwargs["amounts"] == [1000.0]
    assert kwargs["currency"] == "KES"
    assert kwargs["notes"] == [None]


def test_create_allocations_with_a_categorized_split(client, fake_user, fake_pool, monkeypatch):
    monkeypatch.setattr("app.api.transactions.get_transaction", AsyncMock(return_value=TRANSACTION_ROW))
    monkeypatch.setattr("app.api.transactions.get_allocations", AsyncMock(return_value=[]))
    monkeypatch.setattr(
        "app.api.transactions.get_categories_by_ids",
        AsyncMock(return_value=[{"id": EXPENSE_CATEGORY_ID, "name": "Transport", "type": "expense"}]),
    )
    monkeypatch.setattr("app.api.transactions.insert_allocations", AsyncMock(return_value=None))

    response = client.post(
        f"/transactions/{TRANSACTION_ID}/allocations",
        json=request_body(
            allocations=[{"category_id": str(EXPENSE_CATEGORY_ID), "amount": 1000.0, "note": "Fare to town"}]
        ),
    )

    assert response.status_code == 201


def test_create_allocations_with_a_hybrid_split_in_one_call(client, fake_user, fake_pool, monkeypatch):
    monkeypatch.setattr("app.api.transactions.get_transaction", AsyncMock(return_value=TRANSACTION_ROW))
    monkeypatch.setattr("app.api.transactions.get_allocations", AsyncMock(return_value=[]))
    monkeypatch.setattr(
        "app.api.transactions.get_categories_by_ids",
        AsyncMock(return_value=[{"id": EXPENSE_CATEGORY_ID, "name": "Transport", "type": "expense"}]),
    )
    mock_insert = AsyncMock(return_value=None)
    monkeypatch.setattr("app.api.transactions.insert_allocations", mock_insert)

    response = client.post(
        f"/transactions/{TRANSACTION_ID}/allocations",
        json=request_body(
            allocations=[
                {"category_id": str(EXPENSE_CATEGORY_ID), "amount": 600.0, "note": None},
                {"category_id": None, "amount": 400.0, "note": "Cash gift"},
            ]
        ),
    )

    assert response.status_code == 201
    _, kwargs = mock_insert.call_args
    assert kwargs["amounts"] == [600.0, 400.0]


def test_create_allocations_returns_the_refreshed_transaction_and_allocations(
    client, fake_user, fake_pool, monkeypatch
):
    monkeypatch.setattr(
        "app.api.transactions.get_transaction",
        AsyncMock(side_effect=[TRANSACTION_ROW, {**TRANSACTION_ROW, "status": "reconciled"}]),
    )
    monkeypatch.setattr(
        "app.api.transactions.get_allocations",
        AsyncMock(
            side_effect=[
                [],
                [
                    {
                        "id": str(uuid4()),
                        "category_id": None,
                        "category_name": None,
                        "amount": 1000.0,
                        "currency": "KES",
                        "original_amount": 1000.0,
                        "original_currency": "KES",
                        "note": None,
                        "created_at": "2026-09-01T12:00:00Z",
                    }
                ],
            ]
        ),
    )
    monkeypatch.setattr("app.api.transactions.insert_allocations", AsyncMock(return_value=None))

    response = client.post(f"/transactions/{TRANSACTION_ID}/allocations", json=request_body())

    assert response.status_code == 201
    body = response.json()
    assert body["status"] == "reconciled"
    assert len(body["allocations"]) == 1


def test_create_allocations_rejects_both_category_and_transfer_reason_on_one_split(
    client, fake_user, fake_pool, monkeypatch
):
    monkeypatch.setattr("app.api.transactions.get_transaction", AsyncMock(return_value=TRANSACTION_ROW))

    response = client.post(
        f"/transactions/{TRANSACTION_ID}/allocations",
        json=request_body(
            allocations=[
                {
                    "category_id": str(EXPENSE_CATEGORY_ID),
                    "transfer_reason_id": str(uuid4()),
                    "amount": 1000.0,
                    "note": None,
                }
            ]
        ),
    )

    assert response.status_code == 422
    assert "Can't set both a category and a transfer reason" in response.json()["detail"]


def test_create_allocations_rejects_more_than_one_source_on_a_transfer_split(
    client, fake_user, fake_pool, monkeypatch
):
    transfer_reason_id = uuid4()
    monkeypatch.setattr("app.api.transactions.get_transaction", AsyncMock(return_value=TRANSACTION_ROW))
    monkeypatch.setattr(
        "app.api.transactions.get_transfer_reasons_by_ids",
        AsyncMock(return_value=[{"id": transfer_reason_id, "name": "Loan Repayment"}]),
    )

    response = client.post(
        f"/transactions/{TRANSACTION_ID}/allocations",
        json=request_body(
            allocations=[
                {
                    "transfer_reason_id": str(transfer_reason_id),
                    "source_account_id": str(uuid4()),
                    "source_description": "Cash from a friend",
                    "amount": 1000.0,
                    "note": None,
                }
            ]
        ),
    )

    assert response.status_code == 422
    assert "one of an account, a sub-ledger, or a description" in response.json()["detail"]


def test_create_allocations_returns_404_when_transfer_reason_not_found(client, fake_user, fake_pool, monkeypatch):
    monkeypatch.setattr("app.api.transactions.get_transaction", AsyncMock(return_value=TRANSACTION_ROW))
    monkeypatch.setattr("app.api.transactions.get_transfer_reasons_by_ids", AsyncMock(return_value=[]))

    response = client.post(
        f"/transactions/{TRANSACTION_ID}/allocations",
        json=request_body(
            allocations=[{"transfer_reason_id": str(uuid4()), "amount": 1000.0, "note": None}]
        ),
    )

    assert response.status_code == 404
    assert response.json()["detail"] == "Transfer reason not found"


def test_create_allocations_returns_404_when_source_account_not_found(client, fake_user, fake_pool, monkeypatch):
    transfer_reason_id = uuid4()
    monkeypatch.setattr("app.api.transactions.get_transaction", AsyncMock(return_value=TRANSACTION_ROW))
    monkeypatch.setattr(
        "app.api.transactions.get_transfer_reasons_by_ids",
        AsyncMock(return_value=[{"id": transfer_reason_id, "name": "Loan Repayment"}]),
    )
    monkeypatch.setattr("app.api.transactions.get_accounts_by_ids", AsyncMock(return_value=[]))

    response = client.post(
        f"/transactions/{TRANSACTION_ID}/allocations",
        json=request_body(
            allocations=[
                {
                    "transfer_reason_id": str(transfer_reason_id),
                    "source_account_id": str(uuid4()),
                    "amount": 1000.0,
                    "note": None,
                }
            ]
        ),
    )

    assert response.status_code == 404
    assert response.json()["detail"] == "Source account not found"


def test_create_allocations_returns_404_when_source_sub_ledger_not_found(client, fake_user, fake_pool, monkeypatch):
    transfer_reason_id = uuid4()
    monkeypatch.setattr("app.api.transactions.get_transaction", AsyncMock(return_value=TRANSACTION_ROW))
    monkeypatch.setattr(
        "app.api.transactions.get_transfer_reasons_by_ids",
        AsyncMock(return_value=[{"id": transfer_reason_id, "name": "Loan Repayment"}]),
    )
    monkeypatch.setattr("app.api.transactions.get_sub_ledgers_by_ids", AsyncMock(return_value=[]))

    response = client.post(
        f"/transactions/{TRANSACTION_ID}/allocations",
        json=request_body(
            allocations=[
                {
                    "transfer_reason_id": str(transfer_reason_id),
                    "source_sub_ledger_id": str(uuid4()),
                    "amount": 1000.0,
                    "note": None,
                }
            ]
        ),
    )

    assert response.status_code == 404
    assert response.json()["detail"] == "Source sub-ledger not found"


def test_create_allocations_with_a_transfer_shaped_split(client, fake_user, fake_pool, monkeypatch):
    transfer_reason_id = uuid4()
    source_sub_ledger_id = uuid4()
    monkeypatch.setattr("app.api.transactions.get_transaction", AsyncMock(return_value=TRANSACTION_ROW))
    monkeypatch.setattr("app.api.transactions.get_allocations", AsyncMock(return_value=[]))
    monkeypatch.setattr(
        "app.api.transactions.get_transfer_reasons_by_ids",
        AsyncMock(return_value=[{"id": transfer_reason_id, "name": "Loan Repayment"}]),
    )
    monkeypatch.setattr(
        "app.api.transactions.get_sub_ledgers_by_ids",
        AsyncMock(return_value=[{"id": source_sub_ledger_id, "name": "Ordinary Deposit"}]),
    )
    mock_insert = AsyncMock(return_value=None)
    monkeypatch.setattr("app.api.transactions.insert_allocations", mock_insert)

    response = client.post(
        f"/transactions/{TRANSACTION_ID}/allocations",
        json=request_body(
            allocations=[
                {
                    "transfer_reason_id": str(transfer_reason_id),
                    "source_sub_ledger_id": str(source_sub_ledger_id),
                    "amount": 1000.0,
                    "note": None,
                }
            ]
        ),
    )

    assert response.status_code == 201
    _, kwargs = mock_insert.call_args
    assert kwargs["category_ids"] == [None]
    assert kwargs["transfer_reason_ids"] == [transfer_reason_id]
    assert kwargs["source_account_ids"] == [None]
    assert kwargs["source_sub_ledger_ids"] == [source_sub_ledger_id]
    assert kwargs["source_descriptions"] == [None]


def test_create_allocations_with_a_transfer_split_using_a_free_text_source(
    client, fake_user, fake_pool, monkeypatch
):
    transfer_reason_id = uuid4()
    monkeypatch.setattr("app.api.transactions.get_transaction", AsyncMock(return_value=TRANSACTION_ROW))
    monkeypatch.setattr("app.api.transactions.get_allocations", AsyncMock(return_value=[]))
    monkeypatch.setattr(
        "app.api.transactions.get_transfer_reasons_by_ids",
        AsyncMock(return_value=[{"id": transfer_reason_id, "name": "Sent to another of my accounts"}]),
    )
    mock_insert = AsyncMock(return_value=None)
    monkeypatch.setattr("app.api.transactions.insert_allocations", mock_insert)

    response = client.post(
        f"/transactions/{TRANSACTION_ID}/allocations",
        json=request_body(
            allocations=[
                {
                    "transfer_reason_id": str(transfer_reason_id),
                    "source_description": "  Cash from a friend  ",
                    "amount": 1000.0,
                    "note": None,
                }
            ]
        ),
    )

    assert response.status_code == 201
    _, kwargs = mock_insert.call_args
    assert kwargs["source_descriptions"] == ["Cash from a friend"]


def test_create_allocations_returns_generic_error_and_logs_on_unexpected_failure(
    client, fake_user, fake_pool, monkeypatch, caplog
):
    monkeypatch.setattr(
        "app.api.transactions.get_transaction", AsyncMock(side_effect=RuntimeError("connection reset"))
    )

    with caplog.at_level(logging.ERROR):
        response = client.post(f"/transactions/{TRANSACTION_ID}/allocations", json=request_body())

    assert response.status_code == 500
    assert response.json()["detail"] == "Something went wrong. Please try again."
    assert "connection reset" not in response.text
    assert "connection reset" in caplog.text
