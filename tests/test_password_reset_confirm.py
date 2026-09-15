from unittest.mock import AsyncMock

import bcrypt
import pytest
from fastapi.testclient import TestClient

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


def test_rejects_missing_token(client):
    response = client.post(
        "/auth/password-reset/confirm", json={"token": "", "new_password": "password123"}
    )
    assert response.status_code == 422


def test_rejects_short_password(client):
    response = client.post(
        "/auth/password-reset/confirm", json={"token": "sometoken", "new_password": "short"}
    )
    assert response.status_code == 422


def test_rejects_password_over_72_bytes(client):
    response = client.post(
        "/auth/password-reset/confirm",
        json={"token": "sometoken", "new_password": "x" * 73},
    )
    assert response.status_code == 422


def test_returns_503_when_db_unreachable(client):
    response = client.post(
        "/auth/password-reset/confirm",
        json={"token": "sometoken", "new_password": "password123"},
    )
    assert response.status_code == 503


def test_invalid_or_expired_token_returns_400(client, monkeypatch, fake_pool):
    mock_consume = AsyncMock(return_value=False)
    monkeypatch.setattr("app.api.auth.consume_password_reset_token", mock_consume)

    response = client.post(
        "/auth/password-reset/confirm",
        json={"token": "bad-token", "new_password": "password123"},
    )

    assert response.status_code == 400
    assert response.json()["detail"] == "Invalid or expired reset link"


def test_valid_token_resets_the_password(client, monkeypatch, fake_pool):
    mock_consume = AsyncMock(return_value=True)
    monkeypatch.setattr("app.api.auth.consume_password_reset_token", mock_consume)

    response = client.post(
        "/auth/password-reset/confirm",
        json={"token": "good-token", "new_password": "password123"},
    )

    assert response.status_code == 200
    assert response.json()["message"] == "Your password has been reset. You can now log in."

    mock_consume.assert_called_once()
    _, kwargs = mock_consume.call_args
    assert kwargs["token"] == "good-token"
    # New password must be hashed before reaching the repository.
    assert kwargs["new_password_hash"] != "password123"
    assert bcrypt.checkpw(b"password123", kwargs["new_password_hash"].encode())
