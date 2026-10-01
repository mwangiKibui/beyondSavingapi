import logging
from unittest.mock import AsyncMock
from uuid import uuid4

import pytest
from fastapi.testclient import TestClient

from app.core.db import get_pool
from app.core.security import get_current_user_id
from app.main import app

NOTIFICATION_ID = str(uuid4())


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


def test_mark_notification_read_requires_auth(client):
    response = client.patch(f"/notifications/{NOTIFICATION_ID}/read")
    assert response.status_code == 401


def test_mark_notification_read_returns_503_when_db_unreachable(client, fake_user):
    response = client.patch(f"/notifications/{NOTIFICATION_ID}/read")
    assert response.status_code == 503


def test_mark_notification_read_returns_404_when_not_found_or_not_owned(client, fake_user, fake_pool, monkeypatch):
    monkeypatch.setattr("app.api.notifications.mark_notification_read", AsyncMock(return_value=None))

    response = client.patch(f"/notifications/{NOTIFICATION_ID}/read")

    assert response.status_code == 404
    assert response.json()["detail"] == "Notification not found"


def test_mark_notification_read_success(client, fake_user, fake_pool, monkeypatch):
    updated = {
        "id": NOTIFICATION_ID,
        "type": "budget_alert",
        "state": "near",
        "title": "Budget near limit",
        "body": "Foodstuff is approaching its limit.",
        "budget_id": str(uuid4()),
        "plan_id": str(uuid4()),
        "read_at": "2026-10-02T08:00:00+00:00",
        "created_at": "2026-10-01T12:00:00+00:00",
    }
    mock_mark_read = AsyncMock(return_value=updated)
    monkeypatch.setattr("app.api.notifications.mark_notification_read", mock_mark_read)

    response = client.patch(f"/notifications/{NOTIFICATION_ID}/read")

    assert response.status_code == 200
    body = response.json()
    assert body["read_at"] == "2026-10-02T08:00:00Z"

    _, kwargs = mock_mark_read.call_args
    assert kwargs["user_id"] == fake_user
    assert str(kwargs["notification_id"]) == NOTIFICATION_ID


def test_mark_notification_read_is_idempotent_on_an_already_read_notification(
    client, fake_user, fake_pool, monkeypatch
):
    # The repository's own UPDATE ... COALESCE(read_at, now()) keeps the
    # original read_at - the endpoint just reflects whatever comes back,
    # with no error re-marking an already-read notification.
    already_read = {
        "id": NOTIFICATION_ID,
        "type": "budget_alert",
        "state": "at",
        "title": "Budget at limit",
        "body": "Foodstuff has reached its limit.",
        "budget_id": str(uuid4()),
        "plan_id": str(uuid4()),
        "read_at": "2026-09-30T08:00:00+00:00",
        "created_at": "2026-09-29T12:00:00+00:00",
    }
    monkeypatch.setattr("app.api.notifications.mark_notification_read", AsyncMock(return_value=already_read))

    response = client.patch(f"/notifications/{NOTIFICATION_ID}/read")

    assert response.status_code == 200
    assert response.json()["read_at"] == "2026-09-30T08:00:00Z"


def test_mark_notification_read_logs_and_returns_generic_500_on_unexpected_error(
    client, fake_user, fake_pool, monkeypatch, caplog
):
    monkeypatch.setattr(
        "app.api.notifications.mark_notification_read",
        AsyncMock(side_effect=RuntimeError("connection reset, secret=xyz")),
    )

    with caplog.at_level(logging.ERROR):
        response = client.patch(f"/notifications/{NOTIFICATION_ID}/read")

    assert response.status_code == 500
    body = response.json()
    assert body["detail"] == "Something went wrong. Please try again."
    assert "secret=xyz" not in body["detail"]
