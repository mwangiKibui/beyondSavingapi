import logging
from unittest.mock import AsyncMock
from uuid import uuid4

import pytest
from fastapi.testclient import TestClient

from app.core.db import get_pool
from app.core.security import get_current_user_id
from app.main import app
from app.repositories.transfer_reasons import DuplicateTransferReason

VALID_PAYLOAD = {"name": "Loan Repayment"}


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


def test_create_transfer_reason_requires_auth(client):
    response = client.post("/transfer-reasons", json=VALID_PAYLOAD)
    assert response.status_code == 401


def test_create_transfer_reason_rejects_missing_name(client, fake_user):
    response = client.post("/transfer-reasons", json={"name": ""})
    assert response.status_code == 422


def test_create_transfer_reason_returns_503_when_db_unreachable(client, fake_user):
    response = client.post("/transfer-reasons", json=VALID_PAYLOAD)
    assert response.status_code == 503


def test_create_transfer_reason_success(client, fake_user, fake_pool, monkeypatch):
    created = {
        "id": str(uuid4()),
        "name": "Loan Repayment",
        "is_default": False,
        "created_at": "2026-01-01T00:00:00+00:00",
    }
    mock_create = AsyncMock(return_value=created)
    monkeypatch.setattr("app.api.transfer_reasons.create_transfer_reason", mock_create)

    response = client.post("/transfer-reasons", json=VALID_PAYLOAD)

    assert response.status_code == 201
    body = response.json()
    assert body["name"] == "Loan Repayment"
    assert body["is_default"] is False

    _, kwargs = mock_create.call_args
    assert kwargs["user_id"] == fake_user
    assert kwargs["name"] == "Loan Repayment"


def test_create_transfer_reason_duplicate_returns_409(client, fake_user, fake_pool, monkeypatch):
    mock_create = AsyncMock(side_effect=DuplicateTransferReason())
    monkeypatch.setattr("app.api.transfer_reasons.create_transfer_reason", mock_create)

    response = client.post("/transfer-reasons", json=VALID_PAYLOAD)

    assert response.status_code == 409
    assert "already exists" in response.json()["detail"]


def test_create_transfer_reason_logs_and_returns_generic_500_on_unexpected_error(
    client, fake_user, fake_pool, monkeypatch, caplog
):
    mock_create = AsyncMock(side_effect=RuntimeError("connection reset, secret=xyz"))
    monkeypatch.setattr("app.api.transfer_reasons.create_transfer_reason", mock_create)

    with caplog.at_level(logging.ERROR):
        response = client.post("/transfer-reasons", json=VALID_PAYLOAD)

    assert response.status_code == 500
    body = response.json()
    assert body["detail"] == "Something went wrong. Please try again."
    assert "secret=xyz" not in body["detail"]

    error_records = [r for r in caplog.records if r.levelno == logging.ERROR]
    assert len(error_records) == 1
    assert error_records[0].exc_info is not None
