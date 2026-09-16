import logging
from unittest.mock import AsyncMock
from uuid import uuid4

import pytest
from fastapi.testclient import TestClient

from app.core.db import get_pool
from app.core.security import get_current_user_id
from app.main import app
from app.repositories.categories import DuplicateCategory

CATEGORY_ID = str(uuid4())

EXISTING_CATEGORY = {
    "id": CATEGORY_ID,
    "name": "Groceries",
    "type": "expense",
    "is_default": True,
    "created_at": "2026-01-01T00:00:00+00:00",
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


def test_update_category_requires_auth(client):
    response = client.patch(f"/categories/{CATEGORY_ID}", json={"name": "Food"})
    assert response.status_code == 401


def test_update_category_returns_503_when_db_unreachable(client, fake_user):
    response = client.patch(f"/categories/{CATEGORY_ID}", json={"name": "Food"})
    assert response.status_code == 503


def test_update_category_rejects_empty_name(client, fake_user, fake_pool):
    response = client.patch(f"/categories/{CATEGORY_ID}", json={"name": ""})
    assert response.status_code == 422


def test_update_category_returns_404_when_not_found_or_not_owned(client, fake_user, fake_pool, monkeypatch):
    monkeypatch.setattr("app.api.categories.update_category", AsyncMock(return_value=None))

    response = client.patch(f"/categories/{CATEGORY_ID}", json={"name": "Food"})

    assert response.status_code == 404


def test_update_category_success(client, fake_user, fake_pool, monkeypatch):
    mock_update_category = AsyncMock(return_value={**EXISTING_CATEGORY, "name": "Food"})
    monkeypatch.setattr("app.api.categories.update_category", mock_update_category)

    response = client.patch(f"/categories/{CATEGORY_ID}", json={"name": "Food"})

    assert response.status_code == 200
    assert response.json()["name"] == "Food"

    _, kwargs = mock_update_category.call_args
    assert kwargs["user_id"] == fake_user
    assert kwargs["name"] == "Food"


def test_update_category_allows_renaming_a_default_category(client, fake_user, fake_pool, monkeypatch):
    # is_default is informational, not a permission gate - renaming a
    # seeded default works the same as a user-created one.
    mock_update_category = AsyncMock(return_value={**EXISTING_CATEGORY, "name": "Food", "is_default": True})
    monkeypatch.setattr("app.api.categories.update_category", mock_update_category)

    response = client.patch(f"/categories/{CATEGORY_ID}", json={"name": "Food"})

    assert response.status_code == 200
    assert response.json()["is_default"] is True


def test_update_category_renaming_to_its_own_current_name_succeeds(client, fake_user, fake_pool, monkeypatch):
    monkeypatch.setattr("app.api.categories.update_category", AsyncMock(return_value=EXISTING_CATEGORY))

    response = client.patch(f"/categories/{CATEGORY_ID}", json={"name": "Groceries"})

    assert response.status_code == 200


def test_update_category_duplicate_returns_409(client, fake_user, fake_pool, monkeypatch):
    mock_update_category = AsyncMock(side_effect=DuplicateCategory())
    monkeypatch.setattr("app.api.categories.update_category", mock_update_category)

    response = client.patch(f"/categories/{CATEGORY_ID}", json={"name": "Rent"})

    assert response.status_code == 409
    assert "already exists" in response.json()["detail"]


def test_update_category_logs_and_returns_generic_500_on_unexpected_error(
    client, fake_user, fake_pool, monkeypatch, caplog
):
    monkeypatch.setattr(
        "app.api.categories.update_category",
        AsyncMock(side_effect=RuntimeError("connection reset, secret=xyz")),
    )

    with caplog.at_level(logging.ERROR):
        response = client.patch(f"/categories/{CATEGORY_ID}", json={"name": "Food"})

    assert response.status_code == 500
    body = response.json()
    assert body["detail"] == "Something went wrong. Please try again."
    assert "secret=xyz" not in body["detail"]

    error_records = [r for r in caplog.records if r.levelno == logging.ERROR]
    assert len(error_records) == 1
    assert error_records[0].exc_info is not None
