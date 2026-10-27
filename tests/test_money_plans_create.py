import logging
from unittest.mock import AsyncMock
from uuid import uuid4

import pytest
from fastapi.testclient import TestClient

from app.core.db import get_pool
from app.core.security import get_current_user_id
from app.main import app

VALID_PAYLOAD = {
    "period": "monthly",
    "starts_at": "2026-11-01T00:00:00Z",
    "ends_at": "2026-11-30T23:59:59Z",
}


def _created(**overrides) -> dict:
    return {
        "id": str(uuid4()),
        "name": None,
        "period": "monthly",
        "starts_at": "2026-11-01T00:00:00+00:00",
        "ends_at": "2026-11-30T23:59:59+00:00",
        "currency": "KES",
        "status": "draft",
        "converted_at": None,
        "created_at": "2026-10-27T00:00:00+00:00",
        "updated_at": "2026-10-27T00:00:00+00:00",
        **overrides,
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


def test_create_money_plan_requires_auth(client):
    response = client.post("/money-plans", json=VALID_PAYLOAD)
    assert response.status_code == 401


def test_create_money_plan_rejects_unknown_period(client, fake_user):
    response = client.post("/money-plans", json={**VALID_PAYLOAD, "period": "fortnightly"})
    assert response.status_code == 422


def test_create_money_plan_rejects_end_before_start(client, fake_user):
    response = client.post(
        "/money-plans",
        json={**VALID_PAYLOAD, "starts_at": "2026-12-01T00:00:00Z", "ends_at": "2026-11-01T00:00:00Z"},
    )
    assert response.status_code == 422


def test_create_money_plan_returns_503_when_db_unreachable(client, fake_user):
    response = client.post("/money-plans", json=VALID_PAYLOAD)
    assert response.status_code == 503


def test_create_money_plan_success(client, fake_user, fake_pool, monkeypatch):
    mock_create = AsyncMock(return_value=_created())
    monkeypatch.setattr("app.api.money_plans.create_money_plan", mock_create)

    response = client.post("/money-plans", json=VALID_PAYLOAD)

    assert response.status_code == 201
    body = response.json()
    assert body["status"] == "draft"
    assert body["currency"] == "KES"
    assert body["converted_at"] is None

    _, kwargs = mock_create.call_args
    assert kwargs["user_id"] == fake_user
    assert kwargs["period"] == "monthly"
    assert kwargs["name"] is None


def test_create_money_plan_accepts_custom_period_and_alias(client, fake_user, fake_pool, monkeypatch):
    mock_create = AsyncMock(return_value=_created(name="Back to school", period="custom"))
    monkeypatch.setattr("app.api.money_plans.create_money_plan", mock_create)

    response = client.post(
        "/money-plans", json={**VALID_PAYLOAD, "period": "custom", "name": "  Back to school  "}
    )

    assert response.status_code == 201
    _, kwargs = mock_create.call_args
    assert kwargs["period"] == "custom"
    assert kwargs["name"] == "Back to school"


def test_create_money_plan_treats_blank_alias_as_none(client, fake_user, fake_pool, monkeypatch):
    mock_create = AsyncMock(return_value=_created())
    monkeypatch.setattr("app.api.money_plans.create_money_plan", mock_create)

    response = client.post("/money-plans", json={**VALID_PAYLOAD, "name": "   "})

    assert response.status_code == 201
    _, kwargs = mock_create.call_args
    assert kwargs["name"] is None


def test_create_money_plan_ignores_client_currency(client, fake_user, fake_pool, monkeypatch):
    # MVP1 is single-currency - a client-sent currency is never passed on.
    mock_create = AsyncMock(return_value=_created())
    monkeypatch.setattr("app.api.money_plans.create_money_plan", mock_create)

    response = client.post("/money-plans", json={**VALID_PAYLOAD, "currency": "USD"})

    assert response.status_code == 201
    _, kwargs = mock_create.call_args
    assert "currency" not in kwargs


def test_create_money_plan_returns_generic_500_on_unexpected_error(
    client, fake_user, fake_pool, monkeypatch, caplog
):
    mock_create = AsyncMock(side_effect=RuntimeError("connection reset by peer"))
    monkeypatch.setattr("app.api.money_plans.create_money_plan", mock_create)

    with caplog.at_level(logging.ERROR):
        response = client.post("/money-plans", json=VALID_PAYLOAD)

    assert response.status_code == 500
    assert "connection reset" not in response.text
    assert response.json()["detail"] == "Something went wrong. Please try again."
    assert "connection reset by peer" in caplog.text
