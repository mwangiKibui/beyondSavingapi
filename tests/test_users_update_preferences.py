import logging
from unittest.mock import AsyncMock
from uuid import uuid4

import pytest
from fastapi.testclient import TestClient

from app.core.db import get_pool
from app.core.security import get_current_user_id
from app.main import app

VALID_PAYLOAD = {
    "default_currency": "USD",
    "near_threshold": 0.75,
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


def test_update_preferences_requires_auth(client):
    response = client.patch("/users/me/preferences", json=VALID_PAYLOAD)
    assert response.status_code == 401


def test_update_preferences_returns_503_when_db_unreachable(client, fake_user):
    response = client.patch("/users/me/preferences", json=VALID_PAYLOAD)
    assert response.status_code == 503


def test_update_preferences_rejects_short_currency_code(client, fake_user, fake_pool):
    response = client.patch("/users/me/preferences", json={**VALID_PAYLOAD, "default_currency": "US"})
    assert response.status_code == 422


def test_update_preferences_rejects_long_currency_code(client, fake_user, fake_pool):
    response = client.patch("/users/me/preferences", json={**VALID_PAYLOAD, "default_currency": "USDX"})
    assert response.status_code == 422


def test_update_preferences_rejects_near_threshold_of_zero(client, fake_user, fake_pool):
    response = client.patch("/users/me/preferences", json={**VALID_PAYLOAD, "near_threshold": 0})
    assert response.status_code == 422


def test_update_preferences_rejects_near_threshold_of_one(client, fake_user, fake_pool):
    response = client.patch("/users/me/preferences", json={**VALID_PAYLOAD, "near_threshold": 1})
    assert response.status_code == 422


def test_update_preferences_rejects_negative_near_threshold(client, fake_user, fake_pool):
    response = client.patch("/users/me/preferences", json={**VALID_PAYLOAD, "near_threshold": -0.1})
    assert response.status_code == 422


def test_update_preferences_uppercases_currency_code(client, fake_user, fake_pool, monkeypatch):
    updated = {
        "id": str(fake_user),
        "email": "demo@example.com",
        "first_name": "Demo",
        "last_name": "User",
        "default_currency": "USD",
        "near_threshold": 0.75,
        "created_at": "2026-01-01T00:00:00+00:00",
        "role": "user",
    }
    mock_update = AsyncMock(return_value=updated)
    monkeypatch.setattr("app.api.users.update_user_preferences", mock_update)

    response = client.patch("/users/me/preferences", json={**VALID_PAYLOAD, "default_currency": "usd"})

    assert response.status_code == 200
    _, kwargs = mock_update.call_args
    assert kwargs["default_currency"] == "USD"


def test_update_preferences_returns_404_when_user_not_found(client, fake_user, fake_pool, monkeypatch):
    monkeypatch.setattr("app.api.users.update_user_preferences", AsyncMock(return_value=None))

    response = client.patch("/users/me/preferences", json=VALID_PAYLOAD)

    assert response.status_code == 404


def test_update_preferences_success(client, fake_user, fake_pool, monkeypatch):
    updated = {
        "id": str(fake_user),
        "email": "demo@example.com",
        "first_name": "Demo",
        "last_name": "User",
        "default_currency": "USD",
        "near_threshold": 0.75,
        "created_at": "2026-01-01T00:00:00+00:00",
        "role": "user",
    }
    mock_update = AsyncMock(return_value=updated)
    monkeypatch.setattr("app.api.users.update_user_preferences", mock_update)

    response = client.patch("/users/me/preferences", json=VALID_PAYLOAD)

    assert response.status_code == 200
    body = response.json()
    assert body["default_currency"] == "USD"
    assert body["near_threshold"] == 0.75

    _, kwargs = mock_update.call_args
    assert kwargs["user_id"] == fake_user
    assert kwargs["default_currency"] == "USD"
    assert kwargs["near_threshold"] == 0.75


def test_update_preferences_logs_and_returns_generic_500_on_unexpected_error(
    client, fake_user, fake_pool, monkeypatch, caplog
):
    monkeypatch.setattr(
        "app.api.users.update_user_preferences",
        AsyncMock(side_effect=RuntimeError("connection reset, secret=xyz")),
    )

    with caplog.at_level(logging.ERROR):
        response = client.patch("/users/me/preferences", json=VALID_PAYLOAD)

    assert response.status_code == 500
    body = response.json()
    assert body["detail"] == "Something went wrong. Please try again."
    assert "secret=xyz" not in body["detail"]

    error_records = [r for r in caplog.records if r.levelno == logging.ERROR]
    assert len(error_records) == 1
    assert error_records[0].exc_info is not None
