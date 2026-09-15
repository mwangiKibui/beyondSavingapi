from unittest.mock import AsyncMock
from uuid import uuid4

import bcrypt
import jwt
import pytest
from fastapi.testclient import TestClient

from app.core.config import get_settings
from app.core.db import get_pool
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


def _hashed(password: str) -> str:
    return bcrypt.hashpw(password.encode(), bcrypt.gensalt()).decode()


def test_login_rejects_invalid_email(client):
    response = client.post(
        "/auth/login", json={"email": "not-an-email", "password": "password123"}
    )
    assert response.status_code == 422


def test_login_returns_401_for_unknown_email(client, monkeypatch, fake_pool):
    mock_get_user = AsyncMock(return_value=None)
    monkeypatch.setattr("app.api.auth.get_user_by_email", mock_get_user)

    response = client.post(
        "/auth/login", json={"email": "nobody@example.com", "password": "password123"}
    )

    assert response.status_code == 401
    assert response.json()["detail"] == "Invalid email or password"


def test_login_returns_401_for_wrong_password(client, monkeypatch, fake_pool):
    user_id = uuid4()
    mock_get_user = AsyncMock(
        return_value={"id": user_id, "email": "demo@example.com", "password_hash": _hashed("correct-password")}
    )
    monkeypatch.setattr("app.api.auth.get_user_by_email", mock_get_user)

    response = client.post(
        "/auth/login", json={"email": "demo@example.com", "password": "wrong-password"}
    )

    assert response.status_code == 401
    assert response.json()["detail"] == "Invalid email or password"


def test_login_returns_503_when_db_unreachable(client):
    # No fake_pool override: exercises the real degraded-state path (no
    # Postgres available in this test environment).
    response = client.post(
        "/auth/login", json={"email": "demo@example.com", "password": "password123"}
    )
    assert response.status_code == 503


def test_login_success_returns_valid_jwt(client, monkeypatch, fake_pool):
    user_id = uuid4()
    mock_get_user = AsyncMock(
        return_value={"id": user_id, "email": "demo@example.com", "password_hash": _hashed("password123")}
    )
    monkeypatch.setattr("app.api.auth.get_user_by_email", mock_get_user)

    response = client.post(
        "/auth/login", json={"email": "demo@example.com", "password": "password123"}
    )

    assert response.status_code == 200
    body = response.json()
    assert body["token_type"] == "bearer"
    assert "access_token" in body

    settings = get_settings()
    decoded = jwt.decode(
        body["access_token"], settings.jwt_secret, algorithms=[settings.jwt_algorithm]
    )
    assert decoded["sub"] == str(user_id)
    assert "exp" in decoded
