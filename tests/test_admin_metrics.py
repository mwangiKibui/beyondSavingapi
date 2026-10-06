from unittest.mock import AsyncMock
from uuid import uuid4

import pytest
from fastapi.testclient import TestClient

from app.core.db import get_pool
from app.core.security import get_current_user_id
from app.main import app

METRICS = {
    "total_users": 42,
    "total_accounts": 17,
    "accounts_by_institution": [
        {"provider": "Equity", "account_count": 10},
        {"provider": "Safaricom", "account_count": 5},
        {"provider": "Unknown", "account_count": 2},
    ],
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
        AsyncMock(return_value={"id": user_id, "role": "super_admin"}),
    )
    yield user_id
    app.dependency_overrides.pop(get_current_user_id, None)


def test_metrics_returns_totals_and_institution_breakdown(client, fake_admin, fake_pool, monkeypatch):
    monkeypatch.setattr("app.api.admin.get_admin_metrics", AsyncMock(return_value=METRICS))

    response = client.get("/admin/metrics")

    assert response.status_code == 200
    assert response.json() == METRICS
