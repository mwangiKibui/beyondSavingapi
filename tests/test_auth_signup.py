from unittest.mock import AsyncMock

import bcrypt
import pytest
from fastapi.testclient import TestClient

from app.core.db import get_pool
from app.main import app
from app.repositories.users import EmailAlreadyExists


@pytest.fixture(scope="module")
def client():
    with TestClient(app) as c:
        yield c


@pytest.fixture
def fake_pool():
    """Bypasses the "database unavailable" check; the tests using this
    always mock create_user too, so the fake pool object is never touched."""
    app.dependency_overrides[get_pool] = lambda: object()
    yield
    app.dependency_overrides.pop(get_pool, None)


def test_signup_rejects_invalid_email(client):
    response = client.post(
        "/auth/signup",
        json={"name": "Demo User", "email": "not-an-email", "password": "password123"},
    )
    assert response.status_code == 422


def test_signup_rejects_short_password(client):
    response = client.post(
        "/auth/signup",
        json={"name": "Demo User", "email": "demo@example.com", "password": "short"},
    )
    assert response.status_code == 422


def test_signup_rejects_password_over_72_bytes(client):
    response = client.post(
        "/auth/signup",
        json={
            "name": "Demo User",
            "email": "demo@example.com",
            "password": "x" * 73,
        },
    )
    assert response.status_code == 422


def test_signup_returns_503_when_db_unreachable(client):
    # No fake_pool override here: in this test environment there's no real
    # Postgres, so app.state.db_pool is genuinely None (see main.py's
    # lifespan) - exercising the real degraded-state path.
    response = client.post(
        "/auth/signup",
        json={
            "name": "Demo User",
            "email": "demo@example.com",
            "password": "password123",
        },
    )
    assert response.status_code == 503


def test_signup_success(client, monkeypatch, fake_pool):
    created = {
        "id": "11111111-1111-1111-1111-111111111111",
        "email": "demo@example.com",
        "name": "Demo User",
        "default_currency": "KES",
        "near_threshold": 0.80,
        "created_at": "2026-01-01T00:00:00+00:00",
    }
    mock_create_user = AsyncMock(return_value=created)
    monkeypatch.setattr("app.api.auth.create_user", mock_create_user)

    response = client.post(
        "/auth/signup",
        json={
            "name": "Demo User",
            "email": "demo@example.com",
            "password": "password123",
        },
    )

    assert response.status_code == 201
    body = response.json()
    assert body["email"] == "demo@example.com"
    assert body["name"] == "Demo User"

    # Password must be hashed before reaching the repository, never passed as plaintext.
    _, kwargs = mock_create_user.call_args
    assert kwargs["password_hash"] != "password123"
    assert bcrypt.checkpw(b"password123", kwargs["password_hash"].encode())


def test_signup_duplicate_email_returns_409(client, monkeypatch, fake_pool):
    mock_create_user = AsyncMock(side_effect=EmailAlreadyExists("demo@example.com"))
    monkeypatch.setattr("app.api.auth.create_user", mock_create_user)

    response = client.post(
        "/auth/signup",
        json={
            "name": "Demo User",
            "email": "demo@example.com",
            "password": "password123",
        },
    )

    assert response.status_code == 409
