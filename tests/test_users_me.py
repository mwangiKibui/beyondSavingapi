import logging
from unittest.mock import AsyncMock
from uuid import uuid4

import pytest
from fastapi.testclient import TestClient

from app.core.db import get_pool
from app.core.security import AuthContext, get_auth_context
from app.main import app

USER_PROFILE = {
    "id": str(uuid4()),
    "email": "demo@example.com",
    "first_name": "Demo",
    "last_name": "User",
    "default_currency": "KES",
    "near_threshold": 0.80,
    "created_at": "2026-01-01T00:00:00+00:00",
    "role": "user",
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
    app.dependency_overrides[get_auth_context] = lambda: AuthContext(
        user_id=user_id, impersonated_by=None, imp_session_id=None, read_only=False
    )
    yield user_id
    app.dependency_overrides.pop(get_auth_context, None)


def test_get_me_requires_auth(client):
    response = client.get("/users/me")
    assert response.status_code == 401


def test_get_me_returns_503_when_db_unreachable(client, fake_user):
    response = client.get("/users/me")
    assert response.status_code == 503


def test_get_me_returns_404_when_user_not_found(client, fake_user, fake_pool, monkeypatch):
    monkeypatch.setattr("app.api.users.get_user_by_id", AsyncMock(return_value=None))

    response = client.get("/users/me")

    assert response.status_code == 404


def test_get_me_success(client, fake_user, fake_pool, monkeypatch):
    mock_get = AsyncMock(return_value={**USER_PROFILE, "id": str(fake_user)})
    monkeypatch.setattr("app.api.users.get_user_by_id", mock_get)

    response = client.get("/users/me")

    assert response.status_code == 200
    body = response.json()
    assert body["email"] == "demo@example.com"
    assert body["first_name"] == "Demo"
    assert body["last_name"] == "User"
    assert body["default_currency"] == "KES"
    assert body["near_threshold"] == 0.80
    assert "password_hash" not in body
    assert "password" not in body

    args, _ = mock_get.call_args
    assert args[1] == fake_user


def test_get_me_logs_and_returns_generic_500_on_unexpected_error(client, fake_user, fake_pool, monkeypatch, caplog):
    monkeypatch.setattr(
        "app.api.users.get_user_by_id",
        AsyncMock(side_effect=RuntimeError("connection reset, secret=xyz")),
    )

    with caplog.at_level(logging.ERROR):
        response = client.get("/users/me")

    assert response.status_code == 500
    body = response.json()
    assert body["detail"] == "Something went wrong. Please try again."
    assert "secret=xyz" not in body["detail"]

    error_records = [r for r in caplog.records if r.levelno == logging.ERROR]
    assert len(error_records) == 1
    assert error_records[0].exc_info is not None


def test_get_me_omits_impersonating_for_a_normal_session(client, fake_user, fake_pool, monkeypatch):
    mock_get = AsyncMock(return_value={**USER_PROFILE, "id": str(fake_user)})
    monkeypatch.setattr("app.api.users.get_user_by_id", mock_get)

    response = client.get("/users/me")

    assert response.status_code == 200
    assert response.json()["impersonating"] is None


def test_get_me_includes_impersonating_for_an_impersonation_session(client, fake_pool, monkeypatch):
    target_id = uuid4()
    admin_id = uuid4()
    app.dependency_overrides[get_auth_context] = lambda: AuthContext(
        user_id=target_id, impersonated_by=admin_id, imp_session_id=uuid4(), read_only=True
    )

    async def fake_get_user_by_id(pool, user_id):
        if user_id == target_id:
            return {**USER_PROFILE, "id": str(target_id)}
        if user_id == admin_id:
            return {**USER_PROFILE, "id": str(admin_id), "email": "admin@example.com", "role": "admin"}
        return None

    monkeypatch.setattr("app.api.users.get_user_by_id", AsyncMock(side_effect=fake_get_user_by_id))

    try:
        response = client.get("/users/me")
    finally:
        app.dependency_overrides.pop(get_auth_context, None)

    assert response.status_code == 200
    body = response.json()
    assert body["impersonating"] == {
        "admin_id": str(admin_id),
        "admin_email": "admin@example.com",
        "read_only": True,
    }
