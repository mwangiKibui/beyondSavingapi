"""GET /dashboard (2026-10-03) - mocked at the app.api.dashboard layer,
same convention as every other endpoint test file in this suite.
"""

import logging
from decimal import Decimal
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


def _dashboard():
    return {
        "currency": "KES",
        "total_budget": Decimal("20000.00"),
        "total_in": Decimal("15000.00"),
        "total_out": Decimal("9000.00"),
        "net": Decimal("6000.00"),
        "accounts": [
            {
                "account_id": uuid4(),
                "account_nickname": "Salary Account",
                "money_in": Decimal("15000.00"),
                "money_out": Decimal("9000.00"),
            }
        ],
    }


def test_get_dashboard_requires_auth(client):
    response = client.get("/dashboard", params={"from": "2026-09-01", "to": "2026-09-30"})
    assert response.status_code == 401


def test_get_dashboard_returns_503_when_db_unreachable(client, fake_user):
    response = client.get("/dashboard", params={"from": "2026-09-01", "to": "2026-09-30"})
    assert response.status_code == 503


def test_get_dashboard_requires_from_and_to(client, fake_user, fake_pool):
    response = client.get("/dashboard")
    assert response.status_code == 422


def test_get_dashboard_success_shape(client, fake_user, fake_pool, monkeypatch):
    monkeypatch.setattr("app.api.dashboard.resolve_dashboard_currency", AsyncMock(return_value="KES"))
    monkeypatch.setattr("app.api.dashboard.get_dashboard", AsyncMock(return_value=_dashboard()))

    response = client.get("/dashboard", params={"from": "2026-09-01", "to": "2026-09-30"})

    assert response.status_code == 200
    body = response.json()
    assert body["currency"] == "KES"
    assert body["total_budget"] == "20000.00"
    assert body["total_in"] == "15000.00"
    assert body["total_out"] == "9000.00"
    assert body["net"] == "6000.00"
    assert len(body["accounts"]) == 1
    assert body["accounts"][0]["account_nickname"] == "Salary Account"


def test_get_dashboard_forwards_filters_and_resolved_currency(client, fake_user, fake_pool, monkeypatch):
    monkeypatch.setattr("app.api.dashboard.resolve_dashboard_currency", AsyncMock(return_value="USD"))
    mock = AsyncMock(return_value=_dashboard())
    monkeypatch.setattr("app.api.dashboard.get_dashboard", mock)

    response = client.get(
        "/dashboard", params={"from": "2026-09-01", "to": "2026-09-30", "currency": "USD"}
    )

    assert response.status_code == 200
    _, kwargs = mock.call_args
    assert kwargs["user_id"] == fake_user
    assert kwargs["currency"] == "USD"
    assert str(kwargs["from_date"]) == "2026-09-01"
    assert str(kwargs["to_date"]) == "2026-09-30"


def test_get_dashboard_defaults_currency_when_omitted(client, fake_user, fake_pool, monkeypatch):
    resolve_mock = AsyncMock(return_value="KES")
    monkeypatch.setattr("app.api.dashboard.resolve_dashboard_currency", resolve_mock)
    monkeypatch.setattr("app.api.dashboard.get_dashboard", AsyncMock(return_value=_dashboard()))

    response = client.get("/dashboard", params={"from": "2026-09-01", "to": "2026-09-30"})

    assert response.status_code == 200
    _, kwargs = resolve_mock.call_args
    assert kwargs["currency"] is None


def test_get_dashboard_logs_and_returns_generic_500_on_unexpected_error(
    client, fake_user, fake_pool, monkeypatch, caplog
):
    monkeypatch.setattr("app.api.dashboard.resolve_dashboard_currency", AsyncMock(return_value="KES"))
    monkeypatch.setattr(
        "app.api.dashboard.get_dashboard",
        AsyncMock(side_effect=RuntimeError("connection reset, secret=xyz")),
    )

    with caplog.at_level(logging.ERROR):
        response = client.get("/dashboard", params={"from": "2026-09-01", "to": "2026-09-30"})

    assert response.status_code == 500
    body = response.json()
    assert body["detail"] == "Something went wrong. Please try again."
    assert "secret=xyz" not in body["detail"]
