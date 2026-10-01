import logging
from unittest.mock import AsyncMock
from uuid import uuid4

import pytest
from fastapi.testclient import TestClient

from app.core.db import get_pool
from app.core.security import get_current_user_id
from app.main import app

PLAN_ID = str(uuid4())


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


def test_money_flows_requires_auth(client):
    response = client.get(f"/budget-plans/{PLAN_ID}/money-flows")
    assert response.status_code == 401


def test_money_flows_returns_503_when_db_unreachable(client, fake_user):
    response = client.get(f"/budget-plans/{PLAN_ID}/money-flows")
    assert response.status_code == 503


def test_money_flows_returns_404_when_plan_not_found(client, fake_user, fake_pool, monkeypatch):
    monkeypatch.setattr("app.api.budget_plans.list_plan_money_flows", AsyncMock(return_value=None))

    response = client.get(f"/budget-plans/{PLAN_ID}/money-flows")

    assert response.status_code == 404
    assert "Budget plan not found" in response.json()["detail"]


def test_money_flows_success(client, fake_user, fake_pool, monkeypatch):
    flows = {
        "money_in": [
            {
                "id": str(uuid4()),
                "txn_date": "2026-09-28T00:00:00+00:00",
                "counterparty": "Acme Corp",
                "description": "Payroll",
                "category_name": "Salary",
                "amount": "45000.00",
                "currency": "KES",
            }
        ],
        "money_out": [
            {
                "id": str(uuid4()),
                "txn_date": "2026-09-29T00:00:00+00:00",
                "counterparty": "Naivas",
                "description": None,
                "category_name": "Groceries",
                "amount": "800.00",
                "currency": "KES",
            }
        ],
    }
    monkeypatch.setattr("app.api.budget_plans.list_plan_money_flows", AsyncMock(return_value=flows))

    response = client.get(f"/budget-plans/{PLAN_ID}/money-flows")

    assert response.status_code == 200
    body = response.json()
    assert len(body["money_in"]) == 1
    assert body["money_in"][0]["category_name"] == "Salary"
    assert body["money_in"][0]["amount"] == "45000.00"
    assert len(body["money_out"]) == 1
    assert body["money_out"][0]["amount"] == "800.00"
    assert body["money_out"][0]["description"] is None


def test_money_flows_logs_and_returns_generic_500_on_unexpected_error(
    client, fake_user, fake_pool, monkeypatch, caplog
):
    monkeypatch.setattr(
        "app.api.budget_plans.list_plan_money_flows",
        AsyncMock(side_effect=RuntimeError("connection reset, secret=xyz")),
    )

    with caplog.at_level(logging.ERROR):
        response = client.get(f"/budget-plans/{PLAN_ID}/money-flows")

    assert response.status_code == 500
    body = response.json()
    assert body["detail"] == "Something went wrong. Please try again."
    assert "secret=xyz" not in body["detail"]
