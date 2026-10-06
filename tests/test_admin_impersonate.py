from unittest.mock import AsyncMock
from uuid import uuid4

import jwt
import pytest
from fastapi.testclient import TestClient

from app.core.config import get_settings
from app.core.db import get_pool
from app.core.security import get_current_user_id
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


def _as_admin(monkeypatch, role: str):
    admin_id = uuid4()
    app.dependency_overrides[get_current_user_id] = lambda: admin_id

    async def fake_get_user_by_id(pool, user_id):
        if user_id == admin_id:
            return {"id": admin_id, "role": role}
        return None

    monkeypatch.setattr("app.core.security.get_user_by_id", AsyncMock(side_effect=fake_get_user_by_id))
    return admin_id


def test_impersonate_requires_auth(client):
    response = client.post(f"/admin/users/{uuid4()}/impersonate")
    assert response.status_code == 401


def test_impersonate_rejects_non_admin(client, fake_pool, monkeypatch):
    _as_admin(monkeypatch, "user")
    try:
        response = client.post(f"/admin/users/{uuid4()}/impersonate")
    finally:
        app.dependency_overrides.pop(get_current_user_id, None)
    assert response.status_code == 403


def test_impersonate_returns_404_for_missing_target(client, fake_pool, monkeypatch):
    _as_admin(monkeypatch, "admin")
    monkeypatch.setattr("app.api.admin.get_user_by_id", AsyncMock(return_value=None))
    try:
        response = client.post(f"/admin/users/{uuid4()}/impersonate")
    finally:
        app.dependency_overrides.pop(get_current_user_id, None)
    assert response.status_code == 404


def test_impersonate_returns_409_for_admin_target(client, fake_pool, monkeypatch):
    admin_id = _as_admin(monkeypatch, "super_admin")
    target_id = uuid4()

    async def fake_get_user_by_id(pool, user_id):
        if user_id == admin_id:
            return {"id": admin_id, "role": "super_admin"}
        if user_id == target_id:
            return {"id": target_id, "role": "admin"}
        return None

    monkeypatch.setattr("app.api.admin.get_user_by_id", AsyncMock(side_effect=fake_get_user_by_id))
    try:
        response = client.post(f"/admin/users/{target_id}/impersonate")
    finally:
        app.dependency_overrides.pop(get_current_user_id, None)
    assert response.status_code == 409


@pytest.mark.parametrize("admin_role,expected_read_only", [("admin", True), ("super_admin", False)])
def test_impersonate_mints_token_with_correct_read_only(
    client, fake_pool, monkeypatch, admin_role, expected_read_only
):
    admin_id = _as_admin(monkeypatch, admin_role)
    target_id = uuid4()

    async def fake_get_user_by_id(pool, user_id):
        if user_id == admin_id:
            return {"id": admin_id, "role": admin_role}
        if user_id == target_id:
            return {"id": target_id, "role": "user"}
        return None

    monkeypatch.setattr("app.api.admin.get_user_by_id", AsyncMock(side_effect=fake_get_user_by_id))
    mock_create_session = AsyncMock(return_value=uuid4())
    monkeypatch.setattr("app.api.admin.create_impersonation_session", mock_create_session)

    try:
        response = client.post(f"/admin/users/{target_id}/impersonate")
    finally:
        app.dependency_overrides.pop(get_current_user_id, None)

    assert response.status_code == 200
    body = response.json()
    assert body["read_only"] is expected_read_only
    assert body["token_type"] == "bearer"

    settings = get_settings()
    decoded = jwt.decode(body["access_token"], settings.jwt_secret, algorithms=[settings.jwt_algorithm])
    assert decoded["sub"] == str(target_id)
    assert decoded["impersonated_by"] == str(admin_id)
    assert decoded["read_only"] is expected_read_only
    assert "imp_session_id" in decoded

    _, kwargs = mock_create_session.call_args
    assert kwargs == {"admin_id": admin_id, "target_user_id": target_id}
