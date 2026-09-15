import logging
from unittest.mock import AsyncMock
from uuid import uuid4

import pytest
from fastapi.testclient import TestClient

from app.core.db import get_pool
from app.main import app

RESET_MESSAGE = "If an account exists for that email, a password reset link has been sent."


@pytest.fixture(scope="module")
def client():
    with TestClient(app) as c:
        yield c


@pytest.fixture
def fake_pool():
    app.dependency_overrides[get_pool] = lambda: object()
    yield
    app.dependency_overrides.pop(get_pool, None)


def test_rejects_invalid_email(client):
    response = client.post("/auth/password-reset", json={"email": "not-an-email"})
    assert response.status_code == 422


def test_returns_503_when_db_unreachable(client):
    response = client.post("/auth/password-reset", json={"email": "demo@example.com"})
    assert response.status_code == 503


def test_known_email_creates_a_token_and_returns_generic_message(client, monkeypatch, fake_pool, caplog):
    user_id = uuid4()
    mock_get_user = AsyncMock(return_value={"id": user_id, "email": "demo@example.com"})
    mock_create_token = AsyncMock()
    monkeypatch.setattr("app.api.auth.get_user_by_email", mock_get_user)
    monkeypatch.setattr("app.api.auth.create_password_reset_token", mock_create_token)

    with caplog.at_level(logging.INFO):
        response = client.post("/auth/password-reset", json={"email": "demo@example.com"})

    assert response.status_code == 200
    assert response.json() == {"message": RESET_MESSAGE}

    mock_create_token.assert_called_once()
    _, kwargs = mock_create_token.call_args
    assert kwargs["user_id"] == user_id
    assert isinstance(kwargs["token"], str) and len(kwargs["token"]) > 20

    # Dev-mode stand-in for actually emailing it: logged server-side.
    assert kwargs["token"] in caplog.text


def test_unknown_email_returns_same_generic_message_and_creates_no_token(client, monkeypatch, fake_pool):
    mock_get_user = AsyncMock(return_value=None)
    mock_create_token = AsyncMock()
    monkeypatch.setattr("app.api.auth.get_user_by_email", mock_get_user)
    monkeypatch.setattr("app.api.auth.create_password_reset_token", mock_create_token)

    response = client.post("/auth/password-reset", json={"email": "nobody@example.com"})

    assert response.status_code == 200
    assert response.json() == {"message": RESET_MESSAGE}
    mock_create_token.assert_not_called()
