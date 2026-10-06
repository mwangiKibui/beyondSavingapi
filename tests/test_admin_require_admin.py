from unittest.mock import AsyncMock
from uuid import uuid4

import pytest
from fastapi.testclient import TestClient

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


@pytest.fixture
def fake_user():
    user_id = uuid4()
    app.dependency_overrides[get_current_user_id] = lambda: user_id
    yield user_id
    app.dependency_overrides.pop(get_current_user_id, None)


def test_admin_metrics_requires_auth(client):
    response = client.get("/admin/metrics")
    assert response.status_code == 401


def test_admin_metrics_returns_503_when_db_unreachable(client, fake_user):
    response = client.get("/admin/metrics")
    assert response.status_code == 503


def test_admin_metrics_rejects_regular_user(client, fake_user, fake_pool, monkeypatch):
    monkeypatch.setattr(
        "app.core.security.get_user_by_id",
        AsyncMock(return_value={"id": fake_user, "role": "user"}),
    )
    response = client.get("/admin/metrics")
    assert response.status_code == 403


def test_admin_metrics_rejects_missing_user(client, fake_user, fake_pool, monkeypatch):
    monkeypatch.setattr("app.core.security.get_user_by_id", AsyncMock(return_value=None))
    response = client.get("/admin/metrics")
    assert response.status_code == 403


@pytest.mark.parametrize("role", ["admin", "super_admin"])
def test_admin_metrics_allows_both_admin_tiers(client, fake_user, fake_pool, monkeypatch, role):
    monkeypatch.setattr(
        "app.core.security.get_user_by_id",
        AsyncMock(return_value={"id": fake_user, "role": role}),
    )
    monkeypatch.setattr(
        "app.api.admin.get_admin_metrics",
        AsyncMock(return_value={"total_users": 0, "total_accounts": 0, "accounts_by_institution": []}),
    )
    response = client.get("/admin/metrics")
    assert response.status_code == 200
