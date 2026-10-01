import logging
from unittest.mock import AsyncMock
from uuid import uuid4

import pytest
from fastapi.testclient import TestClient

from app.core.db import get_pool
from app.core.security import get_current_user_id
from app.main import app
from app.repositories.budget_plans import DuplicateBudget

PLAN_ID = str(uuid4())
CATEGORY_ID = str(uuid4())
VALID_PAYLOAD = {"category_id": CATEGORY_ID, "limit_amount": 3000}


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


def test_add_budget_requires_auth(client):
    response = client.post(f"/budget-plans/{PLAN_ID}/budgets", json=VALID_PAYLOAD)
    assert response.status_code == 401


def test_add_budget_rejects_negative_limit(client, fake_user):
    response = client.post(f"/budget-plans/{PLAN_ID}/budgets", json={**VALID_PAYLOAD, "limit_amount": -1})
    assert response.status_code == 422


def test_add_budget_returns_422_when_category_not_found(client, fake_user, fake_pool, monkeypatch):
    monkeypatch.setattr("app.api.budget_plans.get_categories_by_ids", AsyncMock(return_value=[]))

    response = client.post(f"/budget-plans/{PLAN_ID}/budgets", json=VALID_PAYLOAD)

    assert response.status_code == 422
    assert "Category not found" in response.json()["detail"]


def test_add_budget_returns_404_when_plan_not_found(client, fake_user, fake_pool, monkeypatch):
    monkeypatch.setattr(
        "app.api.budget_plans.get_categories_by_ids",
        AsyncMock(return_value=[{"id": CATEGORY_ID, "name": "Foodstuff", "type": "expense"}]),
    )
    monkeypatch.setattr("app.api.budget_plans.add_budget", AsyncMock(return_value=None))

    response = client.post(f"/budget-plans/{PLAN_ID}/budgets", json=VALID_PAYLOAD)

    assert response.status_code == 404
    assert "Budget plan not found" in response.json()["detail"]


def test_add_budget_returns_409_on_duplicate_category(client, fake_user, fake_pool, monkeypatch):
    monkeypatch.setattr(
        "app.api.budget_plans.get_categories_by_ids",
        AsyncMock(return_value=[{"id": CATEGORY_ID, "name": "Foodstuff", "type": "expense"}]),
    )
    monkeypatch.setattr("app.api.budget_plans.add_budget", AsyncMock(side_effect=DuplicateBudget()))

    response = client.post(f"/budget-plans/{PLAN_ID}/budgets", json=VALID_PAYLOAD)

    assert response.status_code == 409
    assert "already exists" in response.json()["detail"]


def test_add_budget_success(client, fake_user, fake_pool, monkeypatch):
    monkeypatch.setattr(
        "app.api.budget_plans.get_categories_by_ids",
        AsyncMock(return_value=[{"id": CATEGORY_ID, "name": "Foodstuff", "type": "expense"}]),
    )
    created = {
        "id": str(uuid4()),
        "plan_id": PLAN_ID,
        "category_id": CATEGORY_ID,
        "limit_amount": "3000.00",
        "created_at": "2026-09-25T00:00:00+00:00",
    }
    mock_add = AsyncMock(return_value=created)
    monkeypatch.setattr("app.api.budget_plans.add_budget", mock_add)

    response = client.post(f"/budget-plans/{PLAN_ID}/budgets", json=VALID_PAYLOAD)

    assert response.status_code == 201
    body = response.json()
    assert body["category_id"] == CATEGORY_ID
    assert body["limit_amount"] == "3000.00"

    _, kwargs = mock_add.call_args
    assert kwargs["user_id"] == fake_user
    assert str(kwargs["plan_id"]) == PLAN_ID
    assert str(kwargs["category_id"]) == CATEGORY_ID


def test_add_budget_allows_null_limit_for_income_category(client, fake_user, fake_pool, monkeypatch):
    monkeypatch.setattr(
        "app.api.budget_plans.get_categories_by_ids",
        AsyncMock(return_value=[{"id": CATEGORY_ID, "name": "Salary", "type": "income"}]),
    )
    created = {
        "id": str(uuid4()),
        "plan_id": PLAN_ID,
        "category_id": CATEGORY_ID,
        "limit_amount": None,
        "created_at": "2026-09-25T00:00:00+00:00",
    }
    mock_add = AsyncMock(return_value=created)
    monkeypatch.setattr("app.api.budget_plans.add_budget", mock_add)

    response = client.post(f"/budget-plans/{PLAN_ID}/budgets", json={"category_id": CATEGORY_ID})

    assert response.status_code == 201
    assert response.json()["limit_amount"] is None

    _, kwargs = mock_add.call_args
    assert kwargs["limit_amount"] is None


def test_add_budget_logs_and_returns_generic_500_on_unexpected_error(
    client, fake_user, fake_pool, monkeypatch, caplog
):
    monkeypatch.setattr(
        "app.api.budget_plans.get_categories_by_ids",
        AsyncMock(side_effect=RuntimeError("connection reset, secret=xyz")),
    )

    with caplog.at_level(logging.ERROR):
        response = client.post(f"/budget-plans/{PLAN_ID}/budgets", json=VALID_PAYLOAD)

    assert response.status_code == 500
    body = response.json()
    assert body["detail"] == "Something went wrong. Please try again."
    assert "secret=xyz" not in body["detail"]
