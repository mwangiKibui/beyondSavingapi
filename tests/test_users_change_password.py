import logging
from unittest.mock import AsyncMock
from uuid import uuid4

import pytest
from fastapi.testclient import TestClient

from app.core.db import get_pool
from app.core.security import get_current_user_id
from app.main import app
from app.repositories.users import IncorrectPassword

VALID_PAYLOAD = {
    "current_password": "oldpassword123",
    "new_password": "newpassword456",
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


def test_change_password_requires_auth(client):
    response = client.patch("/users/me/password", json=VALID_PAYLOAD)
    assert response.status_code == 401


def test_change_password_returns_503_when_db_unreachable(client, fake_user):
    response = client.patch("/users/me/password", json=VALID_PAYLOAD)
    assert response.status_code == 503


def test_change_password_rejects_missing_current_password(client, fake_user, fake_pool):
    response = client.patch("/users/me/password", json={**VALID_PAYLOAD, "current_password": ""})
    assert response.status_code == 422


def test_change_password_rejects_short_new_password(client, fake_user, fake_pool):
    response = client.patch("/users/me/password", json={**VALID_PAYLOAD, "new_password": "short"})
    assert response.status_code == 422


def test_change_password_rejects_new_password_over_72_bytes(client, fake_user, fake_pool):
    response = client.patch("/users/me/password", json={**VALID_PAYLOAD, "new_password": "x" * 73})
    assert response.status_code == 422


def test_change_password_returns_404_when_user_not_found(client, fake_user, fake_pool, monkeypatch):
    monkeypatch.setattr("app.api.users.update_user_password", AsyncMock(return_value=False))

    response = client.patch("/users/me/password", json=VALID_PAYLOAD)

    assert response.status_code == 404


def test_change_password_wrong_current_password_returns_401(client, fake_user, fake_pool, monkeypatch):
    mock_update = AsyncMock(side_effect=IncorrectPassword())
    monkeypatch.setattr("app.api.users.update_user_password", mock_update)

    response = client.patch("/users/me/password", json=VALID_PAYLOAD)

    assert response.status_code == 401
    assert response.json()["detail"] == "Current password is incorrect"


def test_change_password_success(client, fake_user, fake_pool, monkeypatch):
    mock_update = AsyncMock(return_value=True)
    monkeypatch.setattr("app.api.users.update_user_password", mock_update)

    response = client.patch("/users/me/password", json=VALID_PAYLOAD)

    assert response.status_code == 200
    body = response.json()
    assert body == {"ok": True}
    assert "password" not in body
    assert "new_password" not in body
    assert "current_password" not in body

    _, kwargs = mock_update.call_args
    assert kwargs["user_id"] == fake_user
    assert kwargs["current_password"] == "oldpassword123"
    # The new password must be hashed before reaching the repository.
    assert kwargs["new_password_hash"] != "newpassword456"


def test_change_password_logs_and_returns_generic_500_on_unexpected_error(
    client, fake_user, fake_pool, monkeypatch, caplog
):
    monkeypatch.setattr(
        "app.api.users.update_user_password",
        AsyncMock(side_effect=RuntimeError("connection reset, secret=xyz")),
    )

    with caplog.at_level(logging.ERROR):
        response = client.patch("/users/me/password", json=VALID_PAYLOAD)

    assert response.status_code == 500
    body = response.json()
    assert body["detail"] == "Something went wrong. Please try again."
    assert "secret=xyz" not in body["detail"]

    error_records = [r for r in caplog.records if r.levelno == logging.ERROR]
    assert len(error_records) == 1
    assert error_records[0].exc_info is not None
