import logging
from datetime import datetime, timezone
from decimal import Decimal
from unittest.mock import AsyncMock
from uuid import uuid4

import pytest
from fastapi.testclient import TestClient

from app.core.db import get_pool
from app.core.security import get_current_user_id
from app.main import app
from app.repositories.budget_plans import OverlappingBudgetPlan

VALID_PAYLOAD = {
    "period": "weekly",
    "starts_at": "2026-09-25T00:00:00Z",
    "ends_at": "2026-10-02T00:00:00Z",
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


def test_create_budget_plan_requires_auth(client):
    response = client.post("/budget-plans", json=VALID_PAYLOAD)
    assert response.status_code == 401


def test_create_budget_plan_rejects_unknown_period(client, fake_user):
    response = client.post("/budget-plans", json={**VALID_PAYLOAD, "period": "yearly"})
    assert response.status_code == 422


def test_create_budget_plan_rejects_end_before_start(client, fake_user):
    response = client.post(
        "/budget-plans",
        json={**VALID_PAYLOAD, "starts_at": "2026-10-02T00:00:00Z", "ends_at": "2026-09-25T00:00:00Z"},
    )
    assert response.status_code == 422


def test_create_budget_plan_rejects_negative_total_cap(client, fake_user):
    response = client.post("/budget-plans", json={**VALID_PAYLOAD, "total_cap": -5})
    assert response.status_code == 422


def test_create_budget_plan_returns_503_when_db_unreachable(client, fake_user):
    response = client.post("/budget-plans", json=VALID_PAYLOAD)
    assert response.status_code == 503


def test_create_budget_plan_success(client, fake_user, fake_pool, monkeypatch):
    created = {
        "id": str(uuid4()),
        "name": None,
        "period": "weekly",
        "starts_at": "2026-09-25T00:00:00+00:00",
        "ends_at": "2026-10-02T00:00:00+00:00",
        "total_cap": None,
        "currency": "KES",
        "is_recurring": False,
        "is_active": True,
        "created_at": "2026-09-25T00:00:00+00:00",
    }
    mock_create = AsyncMock(return_value=created)
    monkeypatch.setattr("app.api.budget_plans.create_budget_plan", mock_create)

    response = client.post("/budget-plans", json=VALID_PAYLOAD)

    assert response.status_code == 201
    body = response.json()
    assert body["period"] == "weekly"
    assert body["currency"] == "KES"
    assert body["is_recurring"] is False

    _, kwargs = mock_create.call_args
    assert kwargs["user_id"] == fake_user
    assert kwargs["period"] == "weekly"
    assert kwargs["total_cap"] is None
    assert kwargs["is_recurring"] is False


def test_create_budget_plan_accepts_optional_fields(client, fake_user, fake_pool, monkeypatch):
    created = {
        "id": str(uuid4()),
        "name": "This week",
        "period": "weekly",
        "starts_at": "2026-09-25T00:00:00+00:00",
        "ends_at": "2026-10-02T00:00:00+00:00",
        "total_cap": "5000.00",
        "currency": "KES",
        "is_recurring": True,
        "is_active": True,
        "created_at": "2026-09-25T00:00:00+00:00",
    }
    mock_create = AsyncMock(return_value=created)
    monkeypatch.setattr("app.api.budget_plans.create_budget_plan", mock_create)

    response = client.post(
        "/budget-plans",
        json={**VALID_PAYLOAD, "name": "This week", "total_cap": 5000, "is_recurring": True},
    )

    assert response.status_code == 201
    _, kwargs = mock_create.call_args
    assert kwargs["name"] == "This week"
    assert kwargs["total_cap"] == Decimal("5000")
    assert kwargs["is_recurring"] is True


def test_create_budget_plan_returns_409_on_overlapping_same_period_plan(client, fake_user, fake_pool, monkeypatch):
    conflicting_plan = {
        "id": uuid4(),
        "name": "October",
        "starts_at": datetime(2026, 10, 1, tzinfo=timezone.utc),
        "ends_at": datetime(2026, 10, 31, tzinfo=timezone.utc),
    }
    mock_create = AsyncMock(side_effect=OverlappingBudgetPlan(conflicting_plan))
    monkeypatch.setattr("app.api.budget_plans.create_budget_plan", mock_create)

    response = client.post(
        "/budget-plans",
        json={**VALID_PAYLOAD, "period": "monthly", "starts_at": "2026-10-15T00:00:00Z", "ends_at": "2026-11-15T00:00:00Z"},
    )

    assert response.status_code == 409
    detail = response.json()["detail"]
    assert "October" in detail
    assert "monthly" in detail
    assert "Oct 01, 2026" in detail


def test_create_budget_plan_logs_and_returns_generic_500_on_unexpected_error(
    client, fake_user, fake_pool, monkeypatch, caplog
):
    mock_create = AsyncMock(side_effect=RuntimeError("connection reset, secret=xyz"))
    monkeypatch.setattr("app.api.budget_plans.create_budget_plan", mock_create)

    with caplog.at_level(logging.ERROR):
        response = client.post("/budget-plans", json=VALID_PAYLOAD)

    assert response.status_code == 500
    body = response.json()
    assert body["detail"] == "Something went wrong. Please try again."
    assert "secret=xyz" not in body["detail"]
