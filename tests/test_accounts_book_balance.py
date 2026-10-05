from datetime import date
from unittest.mock import AsyncMock
from uuid import uuid4

import pytest
from fastapi.testclient import TestClient

from app.core.db import get_pool
from app.core.security import get_current_user_id
from app.main import app

ACCOUNT_UUID = uuid4()
ACCOUNT_ID = str(ACCOUNT_UUID)


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


def test_book_balance_requires_auth(client):
    response = client.get(f"/accounts/{ACCOUNT_ID}/book-balance")
    assert response.status_code == 401


def test_book_balance_returns_503_when_db_unreachable(client, fake_user):
    response = client.get(f"/accounts/{ACCOUNT_ID}/book-balance")
    assert response.status_code == 503


def test_book_balance_defaults_as_of_to_today(client, fake_user, fake_pool, monkeypatch):
    mock_get_book_balance = AsyncMock(return_value=1250.0)
    monkeypatch.setattr("app.api.accounts.get_book_balance", mock_get_book_balance)

    response = client.get(f"/accounts/{ACCOUNT_ID}/book-balance")

    assert response.status_code == 200
    assert response.json() == {"balance": 1250.0, "as_of": date.today().isoformat()}
    _, kwargs = mock_get_book_balance.call_args
    assert kwargs["account_id"] == ACCOUNT_UUID
    assert kwargs["user_id"] == fake_user
    assert kwargs["as_of"] == date.today()


def test_book_balance_forwards_an_explicit_as_of(client, fake_user, fake_pool, monkeypatch):
    mock_get_book_balance = AsyncMock(return_value=800.0)
    monkeypatch.setattr("app.api.accounts.get_book_balance", mock_get_book_balance)

    response = client.get(f"/accounts/{ACCOUNT_ID}/book-balance", params={"as_of": "2026-09-01"})

    assert response.status_code == 200
    assert response.json() == {"balance": 800.0, "as_of": "2026-09-01"}
    _, kwargs = mock_get_book_balance.call_args
    assert kwargs["as_of"] == date(2026, 9, 1)


def test_book_balance_returns_404_when_not_found_or_not_owned(client, fake_user, fake_pool, monkeypatch):
    monkeypatch.setattr("app.api.accounts.get_book_balance", AsyncMock(return_value=None))

    response = client.get(f"/accounts/{ACCOUNT_ID}/book-balance")

    assert response.status_code == 404


def test_book_balance_returns_500_on_unexpected_error(client, fake_user, fake_pool, monkeypatch, caplog):
    monkeypatch.setattr(
        "app.api.accounts.get_book_balance", AsyncMock(side_effect=RuntimeError("db exploded"))
    )

    response = client.get(f"/accounts/{ACCOUNT_ID}/book-balance")

    assert response.status_code == 500
    assert response.json()["detail"] != "db exploded"
