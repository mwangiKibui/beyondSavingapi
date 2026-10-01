import logging
from unittest.mock import AsyncMock
from uuid import uuid4

import pytest
from fastapi.testclient import TestClient

from app.core.db import get_pool
from app.core.security import get_current_user_id
from app.main import app

BUDGET_ID = str(uuid4())
VALID_PAYLOAD = {"limit_amount": 4000}


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


def test_update_budget_requires_auth(client):
    response = client.patch(f"/budgets/{BUDGET_ID}", json=VALID_PAYLOAD)
    assert response.status_code == 401


def test_update_budget_rejects_negative_limit(client, fake_user):
    response = client.patch(f"/budgets/{BUDGET_ID}", json={"limit_amount": -1})
    assert response.status_code == 422


def test_update_budget_returns_404_when_not_found(client, fake_user, fake_pool, monkeypatch):
    monkeypatch.setattr("app.api.budget_plans.update_budget", AsyncMock(return_value=None))

    response = client.patch(f"/budgets/{BUDGET_ID}", json=VALID_PAYLOAD)

    assert response.status_code == 404
    assert "Budget not found" in response.json()["detail"]


def test_update_budget_success(client, fake_user, fake_pool, monkeypatch):
    updated = {
        "id": BUDGET_ID,
        "plan_id": str(uuid4()),
        "category_id": str(uuid4()),
        "limit_amount": "4000.00",
        "created_at": "2026-09-25T00:00:00+00:00",
    }
    mock_update = AsyncMock(return_value=updated)
    monkeypatch.setattr("app.api.budget_plans.update_budget", mock_update)

    response = client.patch(f"/budgets/{BUDGET_ID}", json=VALID_PAYLOAD)

    assert response.status_code == 200
    assert response.json()["limit_amount"] == "4000.00"

    _, kwargs = mock_update.call_args
    assert kwargs["user_id"] == fake_user
    assert str(kwargs["budget_id"]) == BUDGET_ID


def test_update_budget_allows_null_limit(client, fake_user, fake_pool, monkeypatch):
    updated = {
        "id": BUDGET_ID,
        "plan_id": str(uuid4()),
        "category_id": str(uuid4()),
        "limit_amount": None,
        "created_at": "2026-09-25T00:00:00+00:00",
    }
    mock_update = AsyncMock(return_value=updated)
    monkeypatch.setattr("app.api.budget_plans.update_budget", mock_update)

    response = client.patch(f"/budgets/{BUDGET_ID}", json={"limit_amount": None})

    assert response.status_code == 200
    assert response.json()["limit_amount"] is None

    _, kwargs = mock_update.call_args
    assert kwargs["limit_amount"] is None


def test_update_budget_logs_and_returns_generic_500_on_unexpected_error(
    client, fake_user, fake_pool, monkeypatch, caplog
):
    monkeypatch.setattr(
        "app.api.budget_plans.update_budget", AsyncMock(side_effect=RuntimeError("connection reset, secret=xyz"))
    )

    with caplog.at_level(logging.ERROR):
        response = client.patch(f"/budgets/{BUDGET_ID}", json=VALID_PAYLOAD)

    assert response.status_code == 500
    body = response.json()
    assert body["detail"] == "Something went wrong. Please try again."
    assert "secret=xyz" not in body["detail"]
