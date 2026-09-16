import logging
from unittest.mock import AsyncMock
from uuid import UUID, uuid4

import pytest
from fastapi.testclient import TestClient

from app.core.db import get_pool
from app.core.security import get_current_user_id
from app.main import app

ACCOUNT_ID = str(uuid4())

DEACTIVATED_ACCOUNT = {
    "id": ACCOUNT_ID,
    "nickname": "Equity Salary",
    "account_type": "bank",
    "provider": "Equity Bank",
    "account_number": "1100234501",
    "currency": "KES",
    "is_active": False,
    "created_at": "2026-01-01T00:00:00+00:00",
    "updated_at": "2026-01-01T00:00:00+00:00",
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


def test_deactivate_account_requires_auth(client):
    response = client.post(f"/accounts/{ACCOUNT_ID}/deactivate")
    assert response.status_code == 401


def test_deactivate_account_returns_503_when_db_unreachable(client, fake_user):
    response = client.post(f"/accounts/{ACCOUNT_ID}/deactivate")
    assert response.status_code == 503


def test_deactivate_account_returns_404_when_not_found_or_not_owned(
    client, fake_user, fake_pool, monkeypatch
):
    monkeypatch.setattr("app.api.accounts.deactivate_account", AsyncMock(return_value=None))

    response = client.post(f"/accounts/{ACCOUNT_ID}/deactivate")

    assert response.status_code == 404


def test_deactivate_account_succeeds(client, fake_user, fake_pool, monkeypatch):
    mock_deactivate_account = AsyncMock(return_value=DEACTIVATED_ACCOUNT)
    monkeypatch.setattr("app.api.accounts.deactivate_account", mock_deactivate_account)

    response = client.post(f"/accounts/{ACCOUNT_ID}/deactivate")

    assert response.status_code == 200
    assert response.json()["is_active"] is False

    _, kwargs = mock_deactivate_account.call_args
    assert kwargs["account_id"] == UUID(ACCOUNT_ID)
    assert kwargs["user_id"] == fake_user


def test_deactivate_account_is_idempotent(client, fake_user, fake_pool, monkeypatch):
    # Re-deactivating an already-inactive account is a no-op success, not
    # an error - the UPDATE ... WHERE naturally matches and returns the row
    # regardless of its current is_active value, so this is just confirming
    # the endpoint doesn't special-case or reject that.
    mock_deactivate_account = AsyncMock(return_value=DEACTIVATED_ACCOUNT)
    monkeypatch.setattr("app.api.accounts.deactivate_account", mock_deactivate_account)

    response = client.post(f"/accounts/{ACCOUNT_ID}/deactivate")

    assert response.status_code == 200
    assert response.json()["is_active"] is False


def test_deactivate_account_logs_and_returns_generic_500_on_unexpected_error(
    client, fake_user, fake_pool, monkeypatch, caplog
):
    monkeypatch.setattr(
        "app.api.accounts.deactivate_account",
        AsyncMock(side_effect=RuntimeError("connection reset, secret=xyz")),
    )

    with caplog.at_level(logging.ERROR):
        response = client.post(f"/accounts/{ACCOUNT_ID}/deactivate")

    assert response.status_code == 500
    body = response.json()
    assert body["detail"] == "Something went wrong. Please try again."
    assert "secret=xyz" not in body["detail"]

    error_records = [r for r in caplog.records if r.levelno == logging.ERROR]
    assert len(error_records) == 1
    assert error_records[0].exc_info is not None
