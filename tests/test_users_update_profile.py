import logging
from unittest.mock import AsyncMock
from uuid import uuid4

import pytest
from fastapi.testclient import TestClient

from app.core.db import get_pool
from app.core.security import get_current_user_id
from app.main import app
from app.repositories.users import EmailAlreadyExists

VALID_PAYLOAD = {
    "first_name": "Demo",
    "last_name": "User",
    "email": "demo@example.com",
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


def test_update_profile_requires_auth(client):
    response = client.patch("/users/me", json=VALID_PAYLOAD)
    assert response.status_code == 401


def test_update_profile_returns_503_when_db_unreachable(client, fake_user):
    response = client.patch("/users/me", json=VALID_PAYLOAD)
    assert response.status_code == 503


def test_update_profile_rejects_invalid_email(client, fake_user, fake_pool):
    response = client.patch("/users/me", json={**VALID_PAYLOAD, "email": "not-an-email"})
    assert response.status_code == 422


def test_update_profile_rejects_missing_first_name(client, fake_user, fake_pool):
    response = client.patch("/users/me", json={**VALID_PAYLOAD, "first_name": ""})
    assert response.status_code == 422


def test_update_profile_rejects_missing_last_name(client, fake_user, fake_pool):
    response = client.patch("/users/me", json={**VALID_PAYLOAD, "last_name": ""})
    assert response.status_code == 422


def test_update_profile_requires_all_fields(client, fake_user, fake_pool):
    # Full replace, not a partial update - omitting a field 422s.
    response = client.patch("/users/me", json={"first_name": "Demo"})
    assert response.status_code == 422


def test_update_profile_returns_404_when_user_not_found(client, fake_user, fake_pool, monkeypatch):
    monkeypatch.setattr("app.api.users.update_user_profile", AsyncMock(return_value=None))

    response = client.patch("/users/me", json=VALID_PAYLOAD)

    assert response.status_code == 404


def test_update_profile_success(client, fake_user, fake_pool, monkeypatch):
    updated = {
        "id": str(fake_user),
        "email": "demo@example.com",
        "first_name": "Demo",
        "last_name": "User",
        "default_currency": "KES",
        "near_threshold": 0.80,
        "created_at": "2026-01-01T00:00:00+00:00",
    }
    mock_update = AsyncMock(return_value=updated)
    monkeypatch.setattr("app.api.users.update_user_profile", mock_update)

    response = client.patch("/users/me", json=VALID_PAYLOAD)

    assert response.status_code == 200
    body = response.json()
    assert body["email"] == "demo@example.com"
    assert body["first_name"] == "Demo"

    _, kwargs = mock_update.call_args
    assert kwargs["user_id"] == fake_user
    assert kwargs["email"] == "demo@example.com"
    assert kwargs["first_name"] == "Demo"
    assert kwargs["last_name"] == "User"


def test_update_profile_duplicate_email_returns_409(client, fake_user, fake_pool, monkeypatch):
    mock_update = AsyncMock(side_effect=EmailAlreadyExists("demo@example.com"))
    monkeypatch.setattr("app.api.users.update_user_profile", mock_update)

    response = client.patch("/users/me", json=VALID_PAYLOAD)

    assert response.status_code == 409
    assert "already" in response.json()["detail"].lower()


def test_update_profile_logs_and_returns_generic_500_on_unexpected_error(
    client, fake_user, fake_pool, monkeypatch, caplog
):
    monkeypatch.setattr(
        "app.api.users.update_user_profile",
        AsyncMock(side_effect=RuntimeError("connection reset, secret=xyz")),
    )

    with caplog.at_level(logging.ERROR):
        response = client.patch("/users/me", json=VALID_PAYLOAD)

    assert response.status_code == 500
    body = response.json()
    assert body["detail"] == "Something went wrong. Please try again."
    assert "secret=xyz" not in body["detail"]

    error_records = [r for r in caplog.records if r.levelno == logging.ERROR]
    assert len(error_records) == 1
    assert error_records[0].exc_info is not None
