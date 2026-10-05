from datetime import datetime, timedelta, timezone
from unittest.mock import AsyncMock
from uuid import uuid4

import jwt
import pytest
from fastapi.testclient import TestClient

from app.core.config import get_settings
from app.core.db import get_pool
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


def _token(sub: str, **extra_claims) -> str:
    settings = get_settings()
    payload = {"sub": sub, "exp": datetime.now(timezone.utc) + timedelta(minutes=5), **extra_claims}
    return jwt.encode(payload, settings.jwt_secret, algorithm=settings.jwt_algorithm)


def test_end_impersonation_requires_auth(client):
    response = client.post("/admin/impersonation/end")
    assert response.status_code == 401


def test_end_impersonation_rejects_a_non_impersonation_token(client, fake_pool):
    token = _token(str(uuid4()))
    response = client.post("/admin/impersonation/end", headers={"Authorization": f"Bearer {token}"})
    assert response.status_code == 400


def test_end_impersonation_works_for_a_read_only_session(client, fake_pool, monkeypatch):
    session_id = uuid4()
    token = _token(str(uuid4()), imp_session_id=str(session_id), impersonated_by=str(uuid4()), read_only=True)

    mock_end_session = AsyncMock()
    monkeypatch.setattr("app.api.admin.end_impersonation_session", mock_end_session)

    response = client.post("/admin/impersonation/end", headers={"Authorization": f"Bearer {token}"})

    assert response.status_code == 200
    assert response.json() == {"ok": True}
    _, kwargs = mock_end_session.call_args
    assert kwargs == {"session_id": session_id}


def test_end_impersonation_works_for_a_full_access_session(client, fake_pool, monkeypatch):
    session_id = uuid4()
    token = _token(str(uuid4()), imp_session_id=str(session_id), impersonated_by=str(uuid4()), read_only=False)

    mock_end_session = AsyncMock()
    monkeypatch.setattr("app.api.admin.end_impersonation_session", mock_end_session)

    response = client.post("/admin/impersonation/end", headers={"Authorization": f"Bearer {token}"})

    assert response.status_code == 200
