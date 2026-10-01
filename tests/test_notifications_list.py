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


def test_list_notifications_requires_auth(client):
    response = client.get("/notifications")
    assert response.status_code == 401


def test_list_notifications_returns_503_when_db_unreachable(client, fake_user):
    response = client.get("/notifications")
    assert response.status_code == 503


def test_list_notifications_rejects_an_invalid_page_size(client, fake_user):
    response = client.get("/notifications", params={"page_size": 7})
    assert response.status_code == 422


def test_list_notifications_success(client, fake_user, fake_pool, monkeypatch):
    budget_id = str(uuid4())
    plan_id = str(uuid4())
    items = [
        {
            "id": str(uuid4()),
            "type": "budget_alert",
            "state": "near",
            "title": "Budget near limit",
            "body": '"Foodstuff" is approaching its limit in This week.',
            "budget_id": budget_id,
            "plan_id": plan_id,
            "read_at": None,
            "created_at": "2026-10-01T12:00:00+00:00",
        }
    ]
    mock_list = AsyncMock(return_value=(items, 1, 3))
    monkeypatch.setattr("app.api.notifications.list_notifications", mock_list)

    response = client.get("/notifications", params={"page": 1, "page_size": 10, "unread_only": True})

    assert response.status_code == 200
    body = response.json()
    assert body["total"] == 1
    assert body["page"] == 1
    assert body["page_size"] == 10
    assert body["unread_count"] == 3
    assert body["items"][0]["title"] == "Budget near limit"
    assert body["items"][0]["state"] == "near"
    assert body["items"][0]["read_at"] is None

    _, kwargs = mock_list.call_args
    assert kwargs["user_id"] == fake_user
    assert kwargs["page"] == 1
    assert kwargs["page_size"] == 10
    assert kwargs["unread_only"] is True


def test_list_notifications_defaults_to_all_notifications(client, fake_user, fake_pool, monkeypatch):
    mock_list = AsyncMock(return_value=([], 0, 0))
    monkeypatch.setattr("app.api.notifications.list_notifications", mock_list)

    response = client.get("/notifications")

    assert response.status_code == 200
    _, kwargs = mock_list.call_args
    assert kwargs["unread_only"] is False
    assert kwargs["page"] == 1
    assert kwargs["page_size"] == 10


def test_list_notifications_logs_and_returns_generic_500_on_unexpected_error(
    client, fake_user, fake_pool, monkeypatch, caplog
):
    monkeypatch.setattr(
        "app.api.notifications.list_notifications",
        AsyncMock(side_effect=RuntimeError("connection reset, secret=xyz")),
    )

    with caplog.at_level(logging.ERROR):
        response = client.get("/notifications")

    assert response.status_code == 500
    body = response.json()
    assert body["detail"] == "Something went wrong. Please try again."
    assert "secret=xyz" not in body["detail"]
