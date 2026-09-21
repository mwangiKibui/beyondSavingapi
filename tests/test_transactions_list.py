import logging
from unittest.mock import AsyncMock
from uuid import uuid4

import pytest
from fastapi.testclient import TestClient

from app.core.db import get_pool
from app.core.security import get_current_user_id
from app.main import app

SAMPLE_ITEM = {
    "id": str(uuid4()),
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


def test_list_transactions_requires_auth(client):
    response = client.get("/transactions")
    assert response.status_code == 401


def test_list_transactions_returns_503_when_db_unreachable(client, fake_user):
    response = client.get("/transactions")
    assert response.status_code == 503


def test_list_transactions_defaults(client, fake_user, fake_pool, monkeypatch):
    mock_list = AsyncMock(return_value=([SAMPLE_ITEM], 1))
    monkeypatch.setattr("app.api.transactions.list_transactions", mock_list)

    response = client.get("/transactions")

    assert response.status_code == 200
    body = response.json()
    assert body == {"items": [SAMPLE_ITEM], "total": 1, "page": 1, "page_size": 10}

    _, kwargs = mock_list.call_args
    assert kwargs["user_id"] == fake_user
    assert kwargs["account_id"] is None
    assert kwargs["from_date"] is None
    assert kwargs["to_date"] is None
    assert kwargs["direction"] is None
    assert kwargs["status"] is None
    assert kwargs["import_id"] is None
    assert kwargs["search"] is None
    assert kwargs["page"] == 1
    assert kwargs["page_size"] == 10
    assert kwargs["sort_by"] == "txn_date"
    assert kwargs["sort_dir"] == "desc"


def test_list_transactions_forwards_all_query_params(client, fake_user, fake_pool, monkeypatch):
    monkeypatch.setattr("app.api.transactions.get_account", AsyncMock(return_value={"id": "x"}))
    mock_list = AsyncMock(return_value=([], 0))
    monkeypatch.setattr("app.api.transactions.list_transactions", mock_list)
    account_id = uuid4()

    response = client.get(
        "/transactions",
        params={
            "account_id": str(account_id),
            "from": "2026-08-01",
            "to": "2026-08-31",
            "direction": "in",
            "status": "unreconciled",
            "search": "naivas",
            "page": 2,
            "page_size": 20,
            "sort_by": "amount",
            "sort_dir": "asc",
        },
    )

    assert response.status_code == 200
    _, kwargs = mock_list.call_args
    assert kwargs["account_id"] == account_id
    assert str(kwargs["from_date"]) == "2026-08-01"
    assert str(kwargs["to_date"]) == "2026-08-31"
    assert kwargs["direction"] == "in"
    assert kwargs["status"] == "unreconciled"
    assert kwargs["search"] == "naivas"
    assert kwargs["page"] == 2
    assert kwargs["page_size"] == 20
    assert kwargs["sort_by"] == "amount"
    assert kwargs["sort_dir"] == "asc"


def test_list_transactions_rejects_an_account_not_owned_by_the_user(client, fake_user, fake_pool, monkeypatch):
    monkeypatch.setattr("app.api.transactions.get_account", AsyncMock(return_value=None))

    response = client.get("/transactions", params={"account_id": str(uuid4())})

    assert response.status_code == 404


def test_list_transactions_filters_by_import_id(client, fake_user, fake_pool, monkeypatch):
    monkeypatch.setattr(
        "app.api.transactions.get_import_owned_by_user", AsyncMock(return_value={"id": "x"})
    )
    mock_list = AsyncMock(return_value=([SAMPLE_ITEM], 1))
    monkeypatch.setattr("app.api.transactions.list_transactions", mock_list)
    import_id = uuid4()

    response = client.get("/transactions", params={"import_id": str(import_id)})

    assert response.status_code == 200
    _, kwargs = mock_list.call_args
    assert kwargs["import_id"] == import_id


def test_list_transactions_rejects_an_import_not_owned_by_the_user(client, fake_user, fake_pool, monkeypatch):
    monkeypatch.setattr("app.api.transactions.get_import_owned_by_user", AsyncMock(return_value=None))

    response = client.get("/transactions", params={"import_id": str(uuid4())})

    assert response.status_code == 404


@pytest.mark.parametrize(
    "params",
    [
        {"page_size": 15},
        {"direction": "sideways"},
        {"status": "partial"},
        {"sort_by": "counterparty"},
        {"sort_dir": "upwards"},
        {"account_id": "not-a-uuid"},
        {"import_id": "not-a-uuid"},
        {"from": "not-a-date"},
        {"page": 0},
    ],
)
def test_list_transactions_rejects_invalid_query_params(client, fake_user, params):
    response = client.get("/transactions", params=params)
    assert response.status_code == 422


def test_list_transactions_logs_and_returns_generic_500_on_unexpected_error(
    client, fake_user, fake_pool, monkeypatch, caplog
):
    mock_list = AsyncMock(side_effect=RuntimeError("connection reset, secret=xyz"))
    monkeypatch.setattr("app.api.transactions.list_transactions", mock_list)

    with caplog.at_level(logging.ERROR):
        response = client.get("/transactions")

    assert response.status_code == 500
    body = response.json()
    assert body["detail"] == "Something went wrong. Please try again."
    assert "secret=xyz" not in body["detail"]

    error_records = [r for r in caplog.records if r.levelno == logging.ERROR]
    assert len(error_records) == 1
    assert error_records[0].exc_info is not None
