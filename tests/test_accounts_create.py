import logging
from unittest.mock import AsyncMock
from uuid import uuid4

import pytest
from fastapi.testclient import TestClient

from app.core.db import get_pool
from app.core.security import get_current_user_id
from app.main import app
from app.repositories.accounts import DuplicateAccount

VALID_PAYLOAD = {
    "nickname": "Equity Salary",
    "account_type": "bank",
    "provider": "Equity Bank",
    "account_number": "1100234501",
    "currency": "KES",
}


@pytest.fixture(scope="module")
def client():
    with TestClient(app) as c:
        yield c


@pytest.fixture
def fake_pool():
    """Bypasses the "database unavailable" check; tests using this always
    mock create_account too, so the fake pool object is never touched."""
    app.dependency_overrides[get_pool] = lambda: object()
    yield
    app.dependency_overrides.pop(get_pool, None)


@pytest.fixture
def fake_user():
    user_id = uuid4()
    app.dependency_overrides[get_current_user_id] = lambda: user_id
    yield user_id
    app.dependency_overrides.pop(get_current_user_id, None)


def test_create_account_requires_auth(client):
    response = client.post("/accounts", json=VALID_PAYLOAD)
    assert response.status_code == 401


def test_create_account_rejects_missing_nickname(client, fake_user):
    response = client.post("/accounts", json={**VALID_PAYLOAD, "nickname": ""})
    assert response.status_code == 422


def test_create_account_rejects_unknown_account_type(client, fake_user):
    response = client.post("/accounts", json={**VALID_PAYLOAD, "account_type": "crypto_wallet"})
    assert response.status_code == 422


def test_create_account_rejects_unsupported_currency(client, fake_user):
    response = client.post("/accounts", json={**VALID_PAYLOAD, "currency": "JPY"})
    assert response.status_code == 422


def test_create_account_rejects_provider_not_allowed_for_type(client, fake_user, fake_pool):
    response = client.post("/accounts", json={**VALID_PAYLOAD, "provider": "KCB"})
    assert response.status_code == 422


def test_create_account_rejects_provider_valid_for_a_different_type(client, fake_user, fake_pool):
    # "M-Pesa" is a real supported provider, just not for account_type=bank.
    response = client.post("/accounts", json={**VALID_PAYLOAD, "provider": "M-Pesa"})
    assert response.status_code == 422


def test_create_account_rejects_non_digit_account_number(client, fake_user, fake_pool):
    response = client.post("/accounts", json={**VALID_PAYLOAD, "account_number": "abc123"})
    assert response.status_code == 422


def test_create_account_rejects_wrong_length_mobile_number(client, fake_user, fake_pool):
    response = client.post(
        "/accounts",
        json={
            "nickname": "M-Pesa Wallet",
            "account_type": "mobile_money",
            "provider": "M-Pesa",
            "account_number": "12345",
            "currency": "KES",
        },
    )
    assert response.status_code == 422


def test_create_account_accepts_exactly_9_digit_mobile_number(client, fake_user, fake_pool, monkeypatch):
    created = {
        "id": str(uuid4()),
        "nickname": "M-Pesa Wallet",
        "account_type": "mobile_money",
        "provider": "M-Pesa",
        "account_number": "712345678",
        "currency": "KES",
        "is_active": True,
        "created_at": "2026-01-01T00:00:00+00:00",
        "updated_at": "2026-01-01T00:00:00+00:00",
    }
    monkeypatch.setattr("app.api.accounts.create_account", AsyncMock(return_value=created))

    response = client.post(
        "/accounts",
        json={
            "nickname": "M-Pesa Wallet",
            "account_type": "mobile_money",
            "provider": "M-Pesa",
            "account_number": "712345678",
            "currency": "KES",
        },
    )
    assert response.status_code == 201


def test_create_account_rejects_account_number_over_20_digits(client, fake_user, fake_pool):
    response = client.post(
        "/accounts", json={**VALID_PAYLOAD, "account_number": "1" * 21}
    )
    assert response.status_code == 422


def test_create_account_requires_sub_ledger_for_mentor_sacco(client, fake_user, fake_pool):
    response = client.post(
        "/accounts",
        json={
            "nickname": "Mentor Sacco Ordinary",
            "account_type": "sacco",
            "provider": "Mentor Sacco",
            "account_number": "90000001",
            "currency": "KES",
        },
    )
    assert response.status_code == 422
    assert "sub_ledger" in response.json()["detail"]


def test_create_account_rejects_invalid_sub_ledger_value(client, fake_user, fake_pool):
    response = client.post(
        "/accounts",
        json={
            "nickname": "Mentor Sacco Ordinary",
            "account_type": "sacco",
            "provider": "Mentor Sacco",
            "account_number": "90000001",
            "currency": "KES",
            "sub_ledger": "Not A Real Sub-Ledger",
        },
    )
    assert response.status_code == 422


def test_create_account_rejects_sub_ledger_for_non_mentor_sacco_provider(client, fake_user, fake_pool):
    response = client.post(
        "/accounts", json={**VALID_PAYLOAD, "sub_ledger": "Ordinary Deposit"}
    )
    assert response.status_code == 422
    assert "sub_ledger" in response.json()["detail"]


def test_create_account_accepts_valid_mentor_sacco_sub_ledger(client, fake_user, fake_pool, monkeypatch):
    created = {
        "id": str(uuid4()),
        "nickname": "Mentor Sacco Ordinary",
        "account_type": "sacco",
        "provider": "Mentor Sacco",
        "account_number": "90000001",
        "currency": "KES",
        "is_active": True,
        "sub_ledger": "Ordinary Deposit",
        "created_at": "2026-01-01T00:00:00+00:00",
        "updated_at": "2026-01-01T00:00:00+00:00",
    }
    mock_create_account = AsyncMock(return_value=created)
    monkeypatch.setattr("app.api.accounts.create_account", mock_create_account)

    response = client.post(
        "/accounts",
        json={
            "nickname": "Mentor Sacco Ordinary",
            "account_type": "sacco",
            "provider": "Mentor Sacco",
            "account_number": "90000001",
            "currency": "KES",
            "sub_ledger": "Ordinary Deposit",
        },
    )

    assert response.status_code == 201
    assert response.json()["sub_ledger"] == "Ordinary Deposit"
    _, kwargs = mock_create_account.call_args
    assert kwargs["sub_ledger"] == "Ordinary Deposit"


def test_create_account_returns_503_when_db_unreachable(client, fake_user):
    # No fake_pool override: in this test environment there's no real
    # Postgres, so app.state.db_pool is genuinely None.
    response = client.post("/accounts", json=VALID_PAYLOAD)
    assert response.status_code == 503


def test_create_account_success(client, fake_user, fake_pool, monkeypatch):
    created = {
        "id": str(uuid4()),
        "nickname": "Equity Salary",
        "account_type": "bank",
        "provider": "Equity Bank",
        "account_number": "1100234501",
        "currency": "KES",
        "is_active": True,
        "created_at": "2026-01-01T00:00:00+00:00",
        "updated_at": "2026-01-01T00:00:00+00:00",
    }
    mock_create_account = AsyncMock(return_value=created)
    monkeypatch.setattr("app.api.accounts.create_account", mock_create_account)

    response = client.post("/accounts", json=VALID_PAYLOAD)

    assert response.status_code == 201
    body = response.json()
    assert body["nickname"] == "Equity Salary"
    assert body["account_number"] == "1100234501"

    _, kwargs = mock_create_account.call_args
    assert kwargs["user_id"] == fake_user
    assert kwargs["nickname"] == "Equity Salary"
    assert kwargs["account_type"] == "bank"
    assert kwargs["provider"] == "Equity Bank"
    assert kwargs["account_number"] == "1100234501"
    assert kwargs["currency"] == "KES"


def test_create_account_duplicate_returns_409(client, fake_user, fake_pool, monkeypatch):
    mock_create_account = AsyncMock(side_effect=DuplicateAccount("1100234501"))
    monkeypatch.setattr("app.api.accounts.create_account", mock_create_account)

    response = client.post("/accounts", json=VALID_PAYLOAD)

    assert response.status_code == 409
    assert "already exists" in response.json()["detail"]


def test_create_account_logs_and_returns_generic_500_on_unexpected_error(
    client, fake_user, fake_pool, monkeypatch, caplog
):
    mock_create_account = AsyncMock(side_effect=RuntimeError("connection reset by peer, secret=xyz"))
    monkeypatch.setattr("app.api.accounts.create_account", mock_create_account)

    with caplog.at_level(logging.ERROR):
        response = client.post("/accounts", json=VALID_PAYLOAD)

    assert response.status_code == 500
    body = response.json()
    assert body["detail"] == "Something went wrong. Please try again."
    assert "secret=xyz" not in body["detail"]

    # The real exception is logged server-side (with traceback) even
    # though the client only ever sees the generic message above.
    error_records = [r for r in caplog.records if r.levelno == logging.ERROR]
    assert len(error_records) == 1
    assert error_records[0].exc_info is not None
