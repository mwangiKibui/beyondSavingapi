import logging
from unittest.mock import AsyncMock
from uuid import uuid4

import pytest
from fastapi.testclient import TestClient

from app.core.db import get_pool
from app.core.security import get_current_user_id
from app.main import app


def _plan(**overrides) -> dict:
    return {
        "id": str(uuid4()),
        "name": "November salary",
        "period": "monthly",
        "starts_at": "2026-11-01T00:00:00+00:00",
        "ends_at": "2026-11-30T23:59:59+00:00",
        "currency": "KES",
        "status": "draft",
        "converted_at": None,
        "created_at": "2026-10-27T00:00:00+00:00",
        "updated_at": "2026-10-27T00:00:00+00:00",
        "entry_count": 4,
        "summary": {
            "income": "80000.00",
            "spending": "50000.00",
            "loan_repayments": "10000.00",
            "savings": "8000.00",
            "left_over": "12000.00",
            "will_save": "20000.00",
            "save_rate": "25.00",
        },
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


def test_list_money_plans_requires_auth(client):
    response = client.get("/money-plans")
    assert response.status_code == 401


def test_list_money_plans_returns_503_when_db_unreachable(client, fake_user):
    response = client.get("/money-plans")
    assert response.status_code == 503


def test_list_money_plans_returns_plans_with_summary(client, fake_user, fake_pool, monkeypatch):
    mock_list = AsyncMock(return_value=[_plan(), _plan(name=None, status="archived", entry_count=0)])
    monkeypatch.setattr("app.api.money_plans.list_money_plans", mock_list)

    response = client.get("/money-plans")

    assert response.status_code == 200
    items = response.json()["items"]
    assert len(items) == 2
    assert items[0]["entry_count"] == 4
    assert items[0]["summary"]["left_over"] == "12000.00"
    assert items[0]["summary"]["save_rate"] == "25.00"
    assert items[1]["status"] == "archived"

    _, kwargs = mock_list.call_args
    assert kwargs["user_id"] == fake_user
    assert kwargs["status"] is None


def test_list_money_plans_passes_status_filter(client, fake_user, fake_pool, monkeypatch):
    mock_list = AsyncMock(return_value=[])
    monkeypatch.setattr("app.api.money_plans.list_money_plans", mock_list)

    response = client.get("/money-plans", params={"status": "draft"})

    assert response.status_code == 200
    assert response.json() == {"items": []}
    _, kwargs = mock_list.call_args
    assert kwargs["status"] == "draft"


def test_list_money_plans_rejects_unknown_status(client, fake_user, fake_pool):
    response = client.get("/money-plans", params={"status": "deleted"})
    assert response.status_code == 422


def test_list_money_plans_returns_generic_500_on_unexpected_error(
    client, fake_user, fake_pool, monkeypatch, caplog
):
    mock_list = AsyncMock(side_effect=RuntimeError("relation does not exist"))
    monkeypatch.setattr("app.api.money_plans.list_money_plans", mock_list)

    with caplog.at_level(logging.ERROR):
        response = client.get("/money-plans")

    assert response.status_code == 500
    assert response.json()["detail"] == "Something went wrong. Please try again."
    assert "relation does not exist" in caplog.text
