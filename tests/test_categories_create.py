import logging
from unittest.mock import AsyncMock
from uuid import uuid4

import pytest
from fastapi.testclient import TestClient

from app.core.db import get_pool
from app.core.security import get_current_user_id
from app.main import app
from app.repositories.categories import DuplicateCategory

VALID_PAYLOAD = {"name": "Groceries", "type": "expense"}


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


def test_create_category_requires_auth(client):
    response = client.post("/categories", json=VALID_PAYLOAD)
    assert response.status_code == 401


def test_create_category_rejects_missing_name(client, fake_user):
    response = client.post("/categories", json={**VALID_PAYLOAD, "name": ""})
    assert response.status_code == 422


def test_create_category_rejects_unknown_type(client, fake_user):
    response = client.post("/categories", json={**VALID_PAYLOAD, "type": "savings"})
    assert response.status_code == 422


def test_create_category_returns_503_when_db_unreachable(client, fake_user):
    # No fake_pool override: in this test environment there's no real
    # Postgres, so app.state.db_pool is genuinely None.
    response = client.post("/categories", json=VALID_PAYLOAD)
    assert response.status_code == 503


def test_create_category_success(client, fake_user, fake_pool, monkeypatch):
    created = {
        "id": str(uuid4()),
        "name": "Groceries",
        "type": "expense",
        "is_default": False,
        "created_at": "2026-01-01T00:00:00+00:00",
    }
    mock_create_category = AsyncMock(return_value=created)
    monkeypatch.setattr("app.api.categories.create_category", mock_create_category)

    response = client.post("/categories", json=VALID_PAYLOAD)

    assert response.status_code == 201
    body = response.json()
    assert body["name"] == "Groceries"
    assert body["type"] == "expense"
    assert body["is_default"] is False

    _, kwargs = mock_create_category.call_args
    assert kwargs["user_id"] == fake_user
    assert kwargs["name"] == "Groceries"
    assert kwargs["category_type"] == "expense"


def test_create_category_duplicate_returns_409(client, fake_user, fake_pool, monkeypatch):
    mock_create_category = AsyncMock(side_effect=DuplicateCategory())
    monkeypatch.setattr("app.api.categories.create_category", mock_create_category)

    response = client.post("/categories", json=VALID_PAYLOAD)

    assert response.status_code == 409
    assert "already exists" in response.json()["detail"]


def test_create_category_logs_and_returns_generic_500_on_unexpected_error(
    client, fake_user, fake_pool, monkeypatch, caplog
):
    mock_create_category = AsyncMock(side_effect=RuntimeError("connection reset, secret=xyz"))
    monkeypatch.setattr("app.api.categories.create_category", mock_create_category)

    with caplog.at_level(logging.ERROR):
        response = client.post("/categories", json=VALID_PAYLOAD)

    assert response.status_code == 500
    body = response.json()
    assert body["detail"] == "Something went wrong. Please try again."
    assert "secret=xyz" not in body["detail"]

    error_records = [r for r in caplog.records if r.levelno == logging.ERROR]
    assert len(error_records) == 1
    assert error_records[0].exc_info is not None
