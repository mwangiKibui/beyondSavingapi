import logging
from unittest.mock import AsyncMock
from uuid import uuid4

import pytest
from fastapi.testclient import TestClient

from app.core.db import get_pool
from app.core.security import get_current_user_id
from app.main import app

ACCOUNT_ID = uuid4()
SUB_LEDGERED_ACCOUNT_ID = uuid4()
SUB_LEDGER_ID = uuid4()
OTHER_SUB_LEDGER_ID = uuid4()

ACCOUNT = {"id": ACCOUNT_ID, "currency": "KES"}
SUB_LEDGERED_ACCOUNT = {"id": SUB_LEDGERED_ACCOUNT_ID, "currency": "KES"}
SUB_LEDGERS = [{"id": SUB_LEDGER_ID, "name": "Instant Loan"}]


def account_for(account_id):
    if account_id == ACCOUNT_ID:
        return ACCOUNT
    if account_id == SUB_LEDGERED_ACCOUNT_ID:
        return SUB_LEDGERED_ACCOUNT
    return None


def sub_ledgers_for(account_id):
    return SUB_LEDGERS if account_id == SUB_LEDGERED_ACCOUNT_ID else []


def simple_row(**overrides):
    return {
        "account_id": str(ACCOUNT_ID),
        "txn_date": "2026-09-01",
        "direction": "out",
        "amount": 500.0,
        "description": "Naivas",
        **overrides,
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


@pytest.fixture
def mocked_repos(monkeypatch):
    get_account_mock = AsyncMock(side_effect=lambda pool, *, account_id, user_id: account_for(account_id))
    list_sub_ledgers_mock = AsyncMock(side_effect=lambda pool, *, account_id: sub_ledgers_for(account_id))
    insert_mock = AsyncMock(return_value=[])
    monkeypatch.setattr("app.api.transactions.get_account", get_account_mock)
    monkeypatch.setattr("app.api.transactions.list_sub_ledgers", list_sub_ledgers_mock)
    monkeypatch.setattr("app.api.transactions.insert_manual_transactions", insert_mock)
    return {"get_account": get_account_mock, "list_sub_ledgers": list_sub_ledgers_mock, "insert": insert_mock}


def test_create_transactions_requires_auth(client):
    response = client.post("/transactions/batch", json={"transactions": [simple_row()]})
    assert response.status_code == 401


def test_create_transactions_returns_503_when_db_unreachable(client, fake_user):
    response = client.post("/transactions/batch", json={"transactions": [simple_row()]})
    assert response.status_code == 503


def test_create_transactions_rejects_an_empty_batch(client, fake_user, fake_pool):
    response = client.post("/transactions/batch", json={"transactions": []})
    assert response.status_code == 422


def test_create_transactions_inserts_a_single_row(client, fake_user, fake_pool, mocked_repos):
    created_id = uuid4()
    mocked_repos["insert"].return_value = [
        {
            "id": created_id,
            "account_id": ACCOUNT_ID,
            "txn_date": "2026-09-01",
            "amount": 500.0,
            "currency": "KES",
            "direction": "out",
            "counterparty": None,
            "description": "Naivas",
            "balance_after": None,
            "import_id": None,
            "sub_ledger_id": None,
        }
    ]

    response = client.post("/transactions/batch", json={"transactions": [simple_row()]})

    assert response.status_code == 201
    body = response.json()
    assert body["items"] == [
        {
            "id": str(created_id),
            "account_id": str(ACCOUNT_ID),
            "txn_date": "2026-09-01",
            "amount": 500.0,
            "currency": "KES",
            "direction": "out",
            "counterparty": None,
            "description": "Naivas",
            "balance_after": None,
            "status": "unreconciled",
            "import_id": None,
            "sub_ledger_id": None,
            "sub_ledger_name": None,
        }
    ]

    _, kwargs = mocked_repos["insert"].call_args
    assert kwargs["account_ids"] == [ACCOUNT_ID]
    assert kwargs["sub_ledger_ids"] == [None]
    assert kwargs["currencies"] == ["KES"]
    assert kwargs["directions"] == ["out"]
    assert kwargs["amounts"] == [500.0]
    assert kwargs["descriptions"] == ["Naivas"]


def test_create_transactions_resolves_sub_ledger_name_for_a_sub_ledgered_account(
    client, fake_user, fake_pool, mocked_repos
):
    created_id = uuid4()
    mocked_repos["insert"].return_value = [
        {
            "id": created_id,
            "account_id": SUB_LEDGERED_ACCOUNT_ID,
            "txn_date": "2026-09-01",
            "amount": 1000.0,
            "currency": "KES",
            "direction": "in",
            "counterparty": None,
            "description": None,
            "balance_after": None,
            "import_id": None,
            "sub_ledger_id": SUB_LEDGER_ID,
        }
    ]

    response = client.post(
        "/transactions/batch",
        json={
            "transactions": [
                simple_row(
                    account_id=str(SUB_LEDGERED_ACCOUNT_ID),
                    sub_ledger_id=str(SUB_LEDGER_ID),
                    direction="in",
                    amount=1000.0,
                    description=None,
                )
            ]
        },
    )

    assert response.status_code == 201
    assert response.json()["items"][0]["sub_ledger_name"] == "Instant Loan"


def test_create_transactions_rejects_an_account_not_owned_by_the_user(client, fake_user, fake_pool, mocked_repos):
    response = client.post(
        "/transactions/batch", json={"transactions": [simple_row(account_id=str(uuid4()))]}
    )

    assert response.status_code == 404
    assert response.json()["detail"] == "Row 1: Account not found"
    mocked_repos["insert"].assert_not_called()


def test_create_transactions_requires_a_sub_ledger_for_a_sub_ledgered_account(
    client, fake_user, fake_pool, mocked_repos
):
    response = client.post(
        "/transactions/batch",
        json={"transactions": [simple_row(account_id=str(SUB_LEDGERED_ACCOUNT_ID))]},
    )

    assert response.status_code == 422
    assert response.json()["detail"] == "Row 1: Sub-ledger is required"
    mocked_repos["insert"].assert_not_called()


def test_create_transactions_rejects_a_sub_ledger_that_doesnt_belong_to_the_account(
    client, fake_user, fake_pool, mocked_repos
):
    response = client.post(
        "/transactions/batch",
        json={
            "transactions": [
                simple_row(account_id=str(SUB_LEDGERED_ACCOUNT_ID), sub_ledger_id=str(OTHER_SUB_LEDGER_ID))
            ]
        },
    )

    assert response.status_code == 422
    assert response.json()["detail"] == "Row 1: Sub-ledger does not belong to this account"


def test_create_transactions_rejects_a_sub_ledger_on_an_account_with_none(
    client, fake_user, fake_pool, mocked_repos
):
    response = client.post(
        "/transactions/batch", json={"transactions": [simple_row(sub_ledger_id=str(SUB_LEDGER_ID))]}
    )

    assert response.status_code == 422
    assert response.json()["detail"] == "Row 1: This account has no sub-ledgers"


def test_create_transactions_rejects_a_future_date(client, fake_user, fake_pool, mocked_repos):
    response = client.post("/transactions/batch", json={"transactions": [simple_row(txn_date="2999-01-01")]})

    assert response.status_code == 422
    assert response.json()["detail"] == "Row 1: Date cannot be in the future"


def test_create_transactions_rejects_an_invalid_direction(client, fake_user, fake_pool, mocked_repos):
    response = client.post("/transactions/batch", json={"transactions": [simple_row(direction="sideways")]})

    assert response.status_code == 422
    assert response.json()["detail"] == "Row 1: Direction must be 'in' or 'out'"


def test_create_transactions_rejects_a_non_positive_amount(client, fake_user, fake_pool, mocked_repos):
    response = client.post("/transactions/batch", json={"transactions": [simple_row(amount=0)]})

    assert response.status_code == 422
    assert response.json()["detail"] == "Row 1: Amount must be greater than zero"


def test_create_transactions_is_all_or_nothing_reporting_the_first_bad_row(
    client, fake_user, fake_pool, mocked_repos
):
    response = client.post(
        "/transactions/batch",
        json={"transactions": [simple_row(), simple_row(amount=-5)]},
    )

    assert response.status_code == 422
    assert response.json()["detail"] == "Row 2: Amount must be greater than zero"
    mocked_repos["insert"].assert_not_called()


def test_create_transactions_logs_and_returns_generic_500_on_unexpected_error(
    client, fake_user, fake_pool, mocked_repos, caplog
):
    mocked_repos["insert"].side_effect = RuntimeError("connection reset, secret=xyz")

    with caplog.at_level(logging.ERROR):
        response = client.post("/transactions/batch", json={"transactions": [simple_row()]})

    assert response.status_code == 500
    body = response.json()
    assert body["detail"] == "Something went wrong. Please try again."
    assert "secret=xyz" not in body["detail"]

    error_records = [r for r in caplog.records if r.levelno == logging.ERROR]
    assert len(error_records) == 1
    assert error_records[0].exc_info is not None
