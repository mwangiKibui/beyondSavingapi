import logging
from unittest.mock import AsyncMock
from uuid import uuid4

import pytest
from fastapi.testclient import TestClient

from app.core.db import get_pool
from app.core.security import get_current_user_id
from app.main import app


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


def test_summary_requires_auth(client):
    response = client.get("/accounts/summary")
    assert response.status_code == 401


def test_summary_returns_503_when_db_unreachable(client, fake_user):
    response = client.get("/accounts/summary")
    assert response.status_code == 503


def test_summary_returns_subtotals_per_currency(client, fake_user, fake_pool, monkeypatch):
    mock_summary = AsyncMock(
        return_value=[
            {"currency": "KES", "account_count": 2, "total": 42000.0},
            {"currency": "USD", "account_count": 1, "total": 500.0},
        ]
    )
    monkeypatch.setattr("app.api.accounts.get_currency_summary", mock_summary)

    response = client.get("/accounts/summary")

    assert response.status_code == 200
    assert response.json() == {
        "subtotals": [
            {"currency": "KES", "account_count": 2, "total": 42000.0},
            {"currency": "USD", "account_count": 1, "total": 500.0},
        ]
    }
    _, kwargs = mock_summary.call_args
    assert kwargs["user_id"] == fake_user


def test_summary_returns_empty_subtotals_when_user_has_no_accounts(client, fake_user, fake_pool, monkeypatch):
    monkeypatch.setattr("app.api.accounts.get_currency_summary", AsyncMock(return_value=[]))

    response = client.get("/accounts/summary")

    assert response.status_code == 200
    assert response.json() == {"subtotals": []}


def test_summary_logs_and_returns_generic_500_on_unexpected_error(
    client, fake_user, fake_pool, monkeypatch, caplog
):
    monkeypatch.setattr(
        "app.api.accounts.get_currency_summary",
        AsyncMock(side_effect=RuntimeError("connection reset, secret=xyz")),
    )

    with caplog.at_level(logging.ERROR):
        response = client.get("/accounts/summary")

    assert response.status_code == 500
    body = response.json()
    assert body["detail"] == "Something went wrong. Please try again."
    assert "secret=xyz" not in body["detail"]

    error_records = [r for r in caplog.records if r.levelno == logging.ERROR]
    assert len(error_records) == 1
    assert error_records[0].exc_info is not None
