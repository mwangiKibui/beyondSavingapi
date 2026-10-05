from unittest.mock import AsyncMock
from uuid import uuid4

import pytest
from fastapi.testclient import TestClient

from app.core.db import get_pool
from app.core.security import get_current_user_id
from app.main import app

SAMPLE_USER = {
    "id": str(uuid4()),
    "email": "demo@example.com",
    "first_name": "Demo",
    "last_name": "User",
    "role": "user",
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
def fake_admin(monkeypatch):
    user_id = uuid4()
    app.dependency_overrides[get_current_user_id] = lambda: user_id
    monkeypatch.setattr(
        "app.core.security.get_user_by_id",
        AsyncMock(return_value={"id": user_id, "role": "admin"}),
    )
    yield user_id
    app.dependency_overrides.pop(get_current_user_id, None)


def test_list_users_requires_auth(client):
    response = client.get("/admin/users")
    assert response.status_code == 401


def test_list_users_rejects_non_admin(client, fake_pool, monkeypatch):
    user_id = uuid4()
    app.dependency_overrides[get_current_user_id] = lambda: user_id
    monkeypatch.setattr(
        "app.core.security.get_user_by_id",
        AsyncMock(return_value={"id": user_id, "role": "user"}),
    )
    try:
        response = client.get("/admin/users")
    finally:
        app.dependency_overrides.pop(get_current_user_id, None)
    assert response.status_code == 403


def test_list_users_default_pagination(client, fake_admin, fake_pool, monkeypatch):
    mock_list_users = AsyncMock(return_value=([SAMPLE_USER], 1))
    monkeypatch.setattr("app.api.admin.list_users", mock_list_users)

    response = client.get("/admin/users")

    assert response.status_code == 200
    body = response.json()
    assert body["items"][0]["id"] == SAMPLE_USER["id"]
    assert body["items"][0]["email"] == SAMPLE_USER["email"]
    assert body["items"][0]["role"] == SAMPLE_USER["role"]
    assert body["total"] == 1
    assert body["page"] == 1
    assert body["page_size"] == 10

    _, kwargs = mock_list_users.call_args
    assert kwargs == {"page": 1, "page_size": 10, "search": None}


def test_list_users_passes_search_and_pagination(client, fake_admin, fake_pool, monkeypatch):
    mock_list_users = AsyncMock(return_value=([], 0))
    monkeypatch.setattr("app.api.admin.list_users", mock_list_users)

    response = client.get("/admin/users", params={"page": 2, "page_size": 25, "search": "demo"})

    assert response.status_code == 200
    _, kwargs = mock_list_users.call_args
    assert kwargs == {"page": 2, "page_size": 25, "search": "demo"}
