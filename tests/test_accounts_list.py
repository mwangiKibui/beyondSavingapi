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
    "nickname": "Equity Salary",
    "account_type": "bank",
    "provider": "Equity Bank",
    "account_number": "1100234501",
    "currency": "KES",
    "is_active": True,
    "balance": 30000.0,
    "unreconciled_count": 3,
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


def test_list_accounts_requires_auth(client):
    response = client.get("/accounts")
    assert response.status_code == 401


def test_list_accounts_returns_503_when_db_unreachable(client, fake_user):
    response = client.get("/accounts")
    assert response.status_code == 503


def test_list_accounts_defaults(client, fake_user, fake_pool, monkeypatch):
    mock_list_accounts = AsyncMock(return_value=([SAMPLE_ITEM], 1))
    monkeypatch.setattr("app.api.accounts.list_accounts", mock_list_accounts)

    response = client.get("/accounts")

    assert response.status_code == 200
    body = response.json()
    assert body == {"items": [SAMPLE_ITEM], "total": 1, "page": 1, "page_size": 10}

    _, kwargs = mock_list_accounts.call_args
    assert kwargs["user_id"] == fake_user
    assert kwargs["page"] == 1
    assert kwargs["page_size"] == 10
    assert kwargs["search"] is None
    assert kwargs["sort_by"] == "nickname"
    assert kwargs["sort_dir"] == "asc"
    assert kwargs["account_type"] is None
    assert kwargs["currency"] is None
    assert kwargs["reconciliation_status"] is None


def test_list_accounts_forwards_all_query_params(client, fake_user, fake_pool, monkeypatch):
    mock_list_accounts = AsyncMock(return_value=([], 0))
    monkeypatch.setattr("app.api.accounts.list_accounts", mock_list_accounts)

    response = client.get(
        "/accounts",
        params={
            "page": 2,
            "page_size": 20,
            "search": "equity",
            "sort_by": "balance",
            "sort_dir": "desc",
            "account_type": "bank",
            "currency": "KES",
            "reconciliation_status": "unreconciled",
        },
    )

    assert response.status_code == 200
    _, kwargs = mock_list_accounts.call_args
    assert kwargs["page"] == 2
    assert kwargs["page_size"] == 20
    assert kwargs["search"] == "equity"
    assert kwargs["sort_by"] == "balance"
    assert kwargs["sort_dir"] == "desc"
    assert kwargs["account_type"] == "bank"
    assert kwargs["currency"] == "KES"
    assert kwargs["reconciliation_status"] == "unreconciled"


@pytest.mark.parametrize(
    "params",
    [
        {"page_size": 15},
        {"sort_by": "type"},
        {"sort_dir": "upwards"},
        {"account_type": "crypto_wallet"},
        {"currency": "JPY"},
        {"reconciliation_status": "partial"},
        {"page": 0},
    ],
)
def test_list_accounts_rejects_invalid_query_params(client, fake_user, params):
    response = client.get("/accounts", params=params)
    assert response.status_code == 422


def test_list_accounts_logs_and_returns_generic_500_on_unexpected_error(
    client, fake_user, fake_pool, monkeypatch, caplog
):
    mock_list_accounts = AsyncMock(side_effect=RuntimeError("connection reset, secret=xyz"))
    monkeypatch.setattr("app.api.accounts.list_accounts", mock_list_accounts)

    with caplog.at_level(logging.ERROR):
        response = client.get("/accounts")

    assert response.status_code == 500
    body = response.json()
    assert body["detail"] == "Something went wrong. Please try again."
    assert "secret=xyz" not in body["detail"]

    error_records = [r for r in caplog.records if r.levelno == logging.ERROR]
    assert len(error_records) == 1
    assert error_records[0].exc_info is not None
