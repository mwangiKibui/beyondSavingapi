import logging
from unittest.mock import AsyncMock
from uuid import uuid4

import pytest
from fastapi.testclient import TestClient

from app.core.db import get_pool
from app.core.security import get_current_user_id
from app.main import app
from app.repositories.accounts import DuplicateAccount

ACCOUNT_ID = str(uuid4())

EXISTING_ACCOUNT = {
    "id": ACCOUNT_ID,
    "nickname": "Equity Salary",
    "account_type": "bank",
    "provider": "Equity Bank",
    "account_number": "1100234501",
    "currency": "KES",
    "is_active": True,
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


def test_update_account_requires_auth(client):
    response = client.patch(f"/accounts/{ACCOUNT_ID}", json={"nickname": "New name"})
    assert response.status_code == 401


def test_update_account_returns_503_when_db_unreachable(client, fake_user):
    response = client.patch(f"/accounts/{ACCOUNT_ID}", json={"nickname": "New name"})
    assert response.status_code == 503


def test_update_account_rejects_empty_payload(client, fake_user, fake_pool):
    response = client.patch(f"/accounts/{ACCOUNT_ID}", json={})
    assert response.status_code == 422


def test_update_account_rejects_account_type_without_provider_and_number(client, fake_user, fake_pool):
    response = client.patch(f"/accounts/{ACCOUNT_ID}", json={"account_type": "sacco"})
    assert response.status_code == 422


def test_update_account_rejects_account_type_without_account_number(client, fake_user, fake_pool):
    response = client.patch(
        f"/accounts/{ACCOUNT_ID}", json={"account_type": "sacco", "provider": "Mentor Sacco"}
    )
    assert response.status_code == 422


def test_update_account_rejects_empty_nickname(client, fake_user, fake_pool):
    response = client.patch(f"/accounts/{ACCOUNT_ID}", json={"nickname": ""})
    assert response.status_code == 422


def test_update_account_returns_404_when_not_found_or_not_owned(client, fake_user, fake_pool, monkeypatch):
    monkeypatch.setattr("app.api.accounts.get_account", AsyncMock(return_value=None))

    response = client.patch(f"/accounts/{ACCOUNT_ID}", json={"nickname": "New name"})

    assert response.status_code == 404


def test_update_nickname_only_does_not_require_provider_revalidation(
    client, fake_user, fake_pool, monkeypatch
):
    mock_get_account = AsyncMock(return_value=EXISTING_ACCOUNT)
    mock_update_account = AsyncMock(return_value={**EXISTING_ACCOUNT, "nickname": "New name"})
    monkeypatch.setattr("app.api.accounts.get_account", mock_get_account)
    monkeypatch.setattr("app.api.accounts.update_account", mock_update_account)

    response = client.patch(f"/accounts/{ACCOUNT_ID}", json={"nickname": "New name"})

    assert response.status_code == 200
    assert response.json()["nickname"] == "New name"
    _, kwargs = mock_update_account.call_args
    assert kwargs["nickname"] == "New name"
    assert kwargs["account_type"] is None
    assert kwargs["provider"] is None
    assert kwargs["account_number"] is None


def test_update_provider_only_validates_against_existing_account_type(
    client, fake_user, fake_pool, monkeypatch
):
    mock_get_account = AsyncMock(return_value=EXISTING_ACCOUNT)  # account_type: bank
    monkeypatch.setattr("app.api.accounts.get_account", mock_get_account)
    monkeypatch.setattr("app.api.accounts.update_account", AsyncMock(return_value=EXISTING_ACCOUNT))

    # "M-Pesa" isn't valid for the existing type (bank).
    response = client.patch(f"/accounts/{ACCOUNT_ID}", json={"provider": "M-Pesa"})
    assert response.status_code == 422


def test_update_account_number_only_validates_against_existing_account_type(
    client, fake_user, fake_pool, monkeypatch
):
    monkeypatch.setattr("app.api.accounts.get_account", AsyncMock(return_value=EXISTING_ACCOUNT))  # bank

    # 5 digits isn't a valid mobile number length, but this account is a
    # bank account (1-20 digits allowed) - existing type governs.
    monkeypatch.setattr("app.api.accounts.update_account", AsyncMock(return_value=EXISTING_ACCOUNT))
    response = client.patch(f"/accounts/{ACCOUNT_ID}", json={"account_number": "12345"})
    assert response.status_code == 200


def test_update_account_number_rejects_non_digits(client, fake_user, fake_pool, monkeypatch):
    monkeypatch.setattr("app.api.accounts.get_account", AsyncMock(return_value=EXISTING_ACCOUNT))

    response = client.patch(f"/accounts/{ACCOUNT_ID}", json={"account_number": "abc123"})
    assert response.status_code == 422


def test_update_account_number_rejects_over_20_digits_for_bank(client, fake_user, fake_pool, monkeypatch):
    monkeypatch.setattr("app.api.accounts.get_account", AsyncMock(return_value=EXISTING_ACCOUNT))

    response = client.patch(f"/accounts/{ACCOUNT_ID}", json={"account_number": "1" * 21})
    assert response.status_code == 422


def test_update_account_type_provider_and_number_together(client, fake_user, fake_pool, monkeypatch):
    mock_get_account = AsyncMock(return_value=EXISTING_ACCOUNT)
    mock_update_account = AsyncMock(
        return_value={
            **EXISTING_ACCOUNT,
            "account_type": "mobile_money",
            "provider": "M-Pesa",
            "account_number": "712345678",
        }
    )
    monkeypatch.setattr("app.api.accounts.get_account", mock_get_account)
    monkeypatch.setattr("app.api.accounts.update_account", mock_update_account)

    response = client.patch(
        f"/accounts/{ACCOUNT_ID}",
        json={"account_type": "mobile_money", "provider": "M-Pesa", "account_number": "712345678"},
    )

    assert response.status_code == 200
    body = response.json()
    assert body["account_type"] == "mobile_money"
    assert body["provider"] == "M-Pesa"
    assert body["account_number"] == "712345678"

    _, kwargs = mock_update_account.call_args
    assert kwargs["account_number"] == "712345678"


def test_update_account_rejects_mobile_number_wrong_length_for_new_type(
    client, fake_user, fake_pool, monkeypatch
):
    monkeypatch.setattr("app.api.accounts.get_account", AsyncMock(return_value=EXISTING_ACCOUNT))

    response = client.patch(
        f"/accounts/{ACCOUNT_ID}",
        json={"account_type": "mobile_money", "provider": "M-Pesa", "account_number": "12345"},
    )

    assert response.status_code == 422


def test_update_account_rejects_provider_not_valid_for_new_type(client, fake_user, fake_pool, monkeypatch):
    monkeypatch.setattr("app.api.accounts.get_account", AsyncMock(return_value=EXISTING_ACCOUNT))

    response = client.patch(
        f"/accounts/{ACCOUNT_ID}",
        json={"account_type": "sacco", "provider": "Equity Bank", "account_number": "1100234501"},
    )

    assert response.status_code == 422


def test_update_account_duplicate_returns_409(client, fake_user, fake_pool, monkeypatch):
    monkeypatch.setattr("app.api.accounts.get_account", AsyncMock(return_value=EXISTING_ACCOUNT))
    monkeypatch.setattr(
        "app.api.accounts.update_account", AsyncMock(side_effect=DuplicateAccount())
    )

    response = client.patch(
        f"/accounts/{ACCOUNT_ID}",
        json={"account_type": "bank", "provider": "Equity Bank", "account_number": "3300123456"},
    )

    assert response.status_code == 409
    assert "already exists" in response.json()["detail"]


def test_update_account_logs_and_returns_generic_500_on_unexpected_error(
    client, fake_user, fake_pool, monkeypatch, caplog
):
    monkeypatch.setattr(
        "app.api.accounts.get_account",
        AsyncMock(side_effect=RuntimeError("connection reset, secret=xyz")),
    )

    with caplog.at_level(logging.ERROR):
        response = client.patch(f"/accounts/{ACCOUNT_ID}", json={"nickname": "New name"})

    assert response.status_code == 500
    body = response.json()
    assert body["detail"] == "Something went wrong. Please try again."
    assert "secret=xyz" not in body["detail"]

    error_records = [r for r in caplog.records if r.levelno == logging.ERROR]
    assert len(error_records) == 1
    assert error_records[0].exc_info is not None
