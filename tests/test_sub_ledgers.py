import logging
from unittest.mock import AsyncMock
from uuid import uuid4

import pytest
from fastapi.testclient import TestClient

from app.core.db import get_pool
from app.core.security import get_current_user_id
from app.main import app
from app.repositories.sub_ledgers import DuplicateSubLedger

ACCOUNT_ID = uuid4()
FAKE_ACCOUNT = {"id": str(ACCOUNT_ID), "nickname": "Mentor Sacco"}
SUB_LEDGER_ID = uuid4()
SUB_LEDGER = {
    "id": SUB_LEDGER_ID,
    "account_id": ACCOUNT_ID,
    "name": "Ordinary Deposit",
    "balance_treatment": "addition",
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


def _base_url(account_id=ACCOUNT_ID):
    return f"/accounts/{account_id}/sub-ledgers"


def test_list_sub_ledgers_requires_auth(client):
    response = client.get(_base_url())
    assert response.status_code == 401


def test_list_sub_ledgers_rejects_account_not_owned(client, fake_user, fake_pool, monkeypatch):
    monkeypatch.setattr("app.api.sub_ledgers.get_account", AsyncMock(return_value=None))
    response = client.get(_base_url())
    assert response.status_code == 404


def test_list_sub_ledgers_returns_items(client, fake_user, fake_pool, monkeypatch):
    monkeypatch.setattr("app.api.sub_ledgers.get_account", AsyncMock(return_value=FAKE_ACCOUNT))
    monkeypatch.setattr("app.api.sub_ledgers.list_sub_ledgers", AsyncMock(return_value=[SUB_LEDGER]))

    response = client.get(_base_url())

    assert response.status_code == 200
    assert response.json()["items"][0]["name"] == "Ordinary Deposit"


def test_create_sub_ledger_success(client, fake_user, fake_pool, monkeypatch):
    monkeypatch.setattr("app.api.sub_ledgers.get_account", AsyncMock(return_value=FAKE_ACCOUNT))
    mock_create = AsyncMock(return_value=SUB_LEDGER)
    monkeypatch.setattr("app.api.sub_ledgers.create_sub_ledger", mock_create)

    response = client.post(
        _base_url(), json={"name": "Ordinary Deposit", "balance_treatment": "addition"}
    )

    assert response.status_code == 201
    assert response.json()["name"] == "Ordinary Deposit"
    _, kwargs = mock_create.call_args
    assert kwargs == {"account_id": ACCOUNT_ID, "name": "Ordinary Deposit", "balance_treatment": "addition"}


def test_create_sub_ledger_rejects_invalid_balance_treatment(client, fake_user, fake_pool, monkeypatch):
    monkeypatch.setattr("app.api.sub_ledgers.get_account", AsyncMock(return_value=FAKE_ACCOUNT))

    response = client.post(_base_url(), json={"name": "Ordinary Deposit", "balance_treatment": "neutral"})

    assert response.status_code == 422


def test_create_sub_ledger_rejects_duplicate_name(client, fake_user, fake_pool, monkeypatch):
    monkeypatch.setattr("app.api.sub_ledgers.get_account", AsyncMock(return_value=FAKE_ACCOUNT))
    monkeypatch.setattr(
        "app.api.sub_ledgers.create_sub_ledger", AsyncMock(side_effect=DuplicateSubLedger("Ordinary Deposit"))
    )

    response = client.post(
        _base_url(), json={"name": "Ordinary Deposit", "balance_treatment": "addition"}
    )

    assert response.status_code == 409


def test_update_sub_ledger_requires_at_least_one_field(client, fake_user, fake_pool, monkeypatch):
    monkeypatch.setattr("app.api.sub_ledgers.get_account", AsyncMock(return_value=FAKE_ACCOUNT))

    response = client.patch(f"{_base_url()}/{SUB_LEDGER_ID}", json={})

    assert response.status_code == 422


def test_update_sub_ledger_renames_it(client, fake_user, fake_pool, monkeypatch):
    monkeypatch.setattr("app.api.sub_ledgers.get_account", AsyncMock(return_value=FAKE_ACCOUNT))
    mock_update = AsyncMock(return_value={**SUB_LEDGER, "name": "Deposits"})
    monkeypatch.setattr("app.api.sub_ledgers.update_sub_ledger", mock_update)

    response = client.patch(f"{_base_url()}/{SUB_LEDGER_ID}", json={"name": "Deposits"})

    assert response.status_code == 200
    assert response.json()["name"] == "Deposits"
    _, kwargs = mock_update.call_args
    assert kwargs == {
        "sub_ledger_id": SUB_LEDGER_ID,
        "account_id": ACCOUNT_ID,
        "name": "Deposits",
        "balance_treatment": None,
    }


def test_update_sub_ledger_returns_404_when_missing(client, fake_user, fake_pool, monkeypatch):
    monkeypatch.setattr("app.api.sub_ledgers.get_account", AsyncMock(return_value=FAKE_ACCOUNT))
    monkeypatch.setattr("app.api.sub_ledgers.update_sub_ledger", AsyncMock(return_value=None))

    response = client.patch(f"{_base_url()}/{SUB_LEDGER_ID}", json={"name": "Deposits"})

    assert response.status_code == 404


def test_delete_sub_ledger_success(client, fake_user, fake_pool, monkeypatch):
    monkeypatch.setattr("app.api.sub_ledgers.get_account", AsyncMock(return_value=FAKE_ACCOUNT))
    monkeypatch.setattr("app.api.sub_ledgers.delete_sub_ledger", AsyncMock(return_value=True))

    response = client.delete(f"{_base_url()}/{SUB_LEDGER_ID}")

    assert response.status_code == 204


def test_delete_sub_ledger_returns_404_when_missing(client, fake_user, fake_pool, monkeypatch):
    monkeypatch.setattr("app.api.sub_ledgers.get_account", AsyncMock(return_value=FAKE_ACCOUNT))
    monkeypatch.setattr("app.api.sub_ledgers.delete_sub_ledger", AsyncMock(return_value=False))

    response = client.delete(f"{_base_url()}/{SUB_LEDGER_ID}")

    assert response.status_code == 404


def test_endpoints_log_and_return_generic_500_on_unexpected_error(client, fake_user, fake_pool, monkeypatch, caplog):
    monkeypatch.setattr(
        "app.api.sub_ledgers.get_account",
        AsyncMock(side_effect=RuntimeError("connection reset by peer, secret=xyz")),
    )

    with caplog.at_level(logging.ERROR):
        response = client.get(_base_url())

    assert response.status_code == 500
    body = response.json()
    assert body["detail"] == "Something went wrong. Please try again."
    assert "secret=xyz" not in body["detail"]
