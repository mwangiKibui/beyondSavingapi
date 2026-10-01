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


def test_list_budget_plans_requires_auth(client):
    response = client.get("/budget-plans")
    assert response.status_code == 401


def test_list_budget_plans_returns_503_when_db_unreachable(client, fake_user):
    response = client.get("/budget-plans")
    assert response.status_code == 503


def test_list_budget_plans_success(client, fake_user, fake_pool, monkeypatch):
    plans = [
        {
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
            "budgets": [
                {
                    "id": str(uuid4()),
                    "category_id": str(uuid4()),
                    "category_name": "Foodstuff",
                    "category_type": "expense",
                    "limit_amount": "3000.00",
                    "consumed": "2460.00",
                    "remaining": "540.00",
                    "percent": "82.00",
                    "state": "near",
                }
            ],
            "total_consumed": "3200.00",
            "total_state": "ok",
            "total_expenditure": "3200.00",
            "total_income": "12000.00",
        }
    ]
    monkeypatch.setattr("app.api.budget_plans.list_budget_plans", AsyncMock(return_value=plans))

    response = client.get("/budget-plans")

    assert response.status_code == 200
    body = response.json()
    assert len(body["items"]) == 1
    plan = body["items"][0]
    assert plan["name"] == "This week"
    assert plan["budgets"][0]["state"] == "near"
    assert plan["budgets"][0]["category_name"] == "Foodstuff"
    assert plan["total_state"] == "ok"
    assert plan["total_expenditure"] == "3200.00"
    assert plan["total_income"] == "12000.00"


def test_list_budget_plans_reports_totals_even_without_a_cap(client, fake_user, fake_pool, monkeypatch):
    plans = [
        {
            "id": str(uuid4()),
            "name": "This week",
            "period": "weekly",
            "starts_at": "2026-09-25T00:00:00+00:00",
            "ends_at": "2026-10-02T00:00:00+00:00",
            "total_cap": None,
            "currency": "KES",
            "is_recurring": True,
            "is_active": True,
            "created_at": "2026-09-25T00:00:00+00:00",
            "budgets": [
                {
                    "id": str(uuid4()),
                    "category_id": str(uuid4()),
                    "category_name": "Salary",
                    "category_type": "income",
                    "limit_amount": None,
                    "consumed": "12000.00",
                    "remaining": None,
                    "percent": None,
                    "state": None,
                }
            ],
            "total_consumed": None,
            "total_state": None,
            "total_expenditure": "0.00",
            "total_income": "12000.00",
        }
    ]
    monkeypatch.setattr("app.api.budget_plans.list_budget_plans", AsyncMock(return_value=plans))

    response = client.get("/budget-plans")

    assert response.status_code == 200
    plan = response.json()["items"][0]
    assert plan["total_cap"] is None
    assert plan["total_state"] is None
    assert plan["total_expenditure"] == "0.00"
    assert plan["total_income"] == "12000.00"
    assert plan["budgets"][0]["limit_amount"] is None
    assert plan["budgets"][0]["state"] is None


def test_list_budget_plans_logs_and_returns_generic_500_on_unexpected_error(
    client, fake_user, fake_pool, monkeypatch, caplog
):
    monkeypatch.setattr(
        "app.api.budget_plans.list_budget_plans", AsyncMock(side_effect=RuntimeError("connection reset, secret=xyz"))
    )

    with caplog.at_level(logging.ERROR):
        response = client.get("/budget-plans")

    assert response.status_code == 500
    body = response.json()
    assert body["detail"] == "Something went wrong. Please try again."
    assert "secret=xyz" not in body["detail"]
