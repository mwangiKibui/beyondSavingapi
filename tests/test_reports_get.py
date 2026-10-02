import logging
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


def _report_payload():
    return {
        "currency": "KES",
        "total_in": "12345.00",
        "total_out": "6789.00",
        "net": "5556.00",
        "by_category": [
            {
                "category_id": str(uuid4()),
                "category_name": "Groceries",
                "category_type": "expense",
                "total": "3000.00",
            }
        ],
        "reconciled_no_category_total": "450.00",
        "unreconciled_total": "1200.00",
    }


def test_get_report_requires_auth(client):
    response = client.get("/reports")
    assert response.status_code == 401


def test_get_report_returns_503_when_db_unreachable(client, fake_user):
    response = client.get("/reports")
    assert response.status_code == 503


def test_get_report_success(client, fake_user, fake_pool, monkeypatch):
    monkeypatch.setattr("app.api.reports.resolve_report_currency", AsyncMock(return_value="KES"))
    monkeypatch.setattr("app.api.reports.get_report", AsyncMock(return_value=_report_payload()))

    response = client.get("/reports")

    assert response.status_code == 200
    body = response.json()
    assert body["currency"] == "KES"
    assert body["total_in"] == "12345.00"
    assert body["total_out"] == "6789.00"
    assert body["net"] == "5556.00"
    assert body["reconciled_no_category_total"] == "450.00"
    assert body["unreconciled_total"] == "1200.00"
    assert body["by_category"][0]["category_name"] == "Groceries"
    assert body["by_category"][0]["category_type"] == "expense"
    assert body["by_category"][0]["total"] == "3000.00"


def test_get_report_defaults_currency_to_users_own_default_when_omitted(client, fake_user, fake_pool, monkeypatch):
    resolve_mock = AsyncMock(return_value="USD")
    get_report_mock = AsyncMock(return_value={**_report_payload(), "currency": "USD"})
    monkeypatch.setattr("app.api.reports.resolve_report_currency", resolve_mock)
    monkeypatch.setattr("app.api.reports.get_report", get_report_mock)

    response = client.get("/reports")

    assert response.status_code == 200
    assert response.json()["currency"] == "USD"
    # The client passed no `currency` query param at all, so the resolver
    # was asked with currency=None (the docs/schema.sql default_currency
    # fallback), and the resolved value is what flows into get_report's
    # own filters.
    _, resolve_kwargs = resolve_mock.call_args
    assert resolve_kwargs["currency"] is None
    filters_arg = get_report_mock.call_args.args[1]
    assert filters_arg.currency == "USD"


def test_get_report_passes_through_explicit_currency_without_resolving_default(
    client, fake_user, fake_pool, monkeypatch
):
    resolve_mock = AsyncMock(return_value="EUR")
    monkeypatch.setattr("app.api.reports.resolve_report_currency", resolve_mock)
    monkeypatch.setattr("app.api.reports.get_report", AsyncMock(return_value={**_report_payload(), "currency": "EUR"}))

    response = client.get("/reports", params={"currency": "EUR"})

    assert response.status_code == 200
    _, resolve_kwargs = resolve_mock.call_args
    assert resolve_kwargs["currency"] == "EUR"


def test_get_report_returns_404_when_account_not_found(client, fake_user, fake_pool, monkeypatch):
    monkeypatch.setattr("app.api.reports.get_account", AsyncMock(return_value=None))

    response = client.get("/reports", params={"account_id": str(uuid4())})

    assert response.status_code == 404
    assert response.json()["detail"] == "Account not found"


def test_get_report_forwards_filters_to_the_repository(client, fake_user, fake_pool, monkeypatch):
    account_id = str(uuid4())
    monkeypatch.setattr(
        "app.api.reports.get_account", AsyncMock(return_value={"id": account_id, "nickname": "KCB Salary"})
    )
    monkeypatch.setattr("app.api.reports.resolve_report_currency", AsyncMock(return_value="KES"))
    get_report_mock = AsyncMock(return_value=_report_payload())
    monkeypatch.setattr("app.api.reports.get_report", get_report_mock)

    response = client.get(
        "/reports",
        params={"account_id": account_id, "from": "2026-09-01", "to": "2026-10-01", "direction": "out"},
    )

    assert response.status_code == 200
    filters_arg = get_report_mock.call_args.args[1]
    assert str(filters_arg.account_id) == account_id
    assert str(filters_arg.from_date) == "2026-09-01"
    assert str(filters_arg.to_date) == "2026-10-01"
    assert filters_arg.direction == "out"


def test_get_report_logs_and_returns_generic_500_on_unexpected_error(client, fake_user, fake_pool, monkeypatch, caplog):
    monkeypatch.setattr("app.api.reports.resolve_report_currency", AsyncMock(return_value="KES"))
    monkeypatch.setattr(
        "app.api.reports.get_report", AsyncMock(side_effect=RuntimeError("connection reset, secret=xyz"))
    )

    with caplog.at_level(logging.ERROR):
        response = client.get("/reports")

    assert response.status_code == 500
    body = response.json()
    assert body["detail"] == "Something went wrong. Please try again."
    assert "secret=xyz" not in body["detail"]


# --- GET /reports/accounts (ab-150) -----------------------------------


def _account_summary_item(**overrides):
    base = {
        "account_id": str(uuid4()),
        "account_nickname": "KCB Salary",
        "money_in": "12345.00",
        "money_out": "6789.00",
    }
    base.update(overrides)
    return base


def test_get_account_summary_requires_auth(client):
    response = client.get("/reports/accounts")
    assert response.status_code == 401


def test_get_account_summary_returns_503_when_db_unreachable(client, fake_user):
    response = client.get("/reports/accounts")
    assert response.status_code == 503


def test_get_account_summary_success(client, fake_user, fake_pool, monkeypatch):
    item = _account_summary_item()
    monkeypatch.setattr("app.api.reports.get_account_summary", AsyncMock(return_value=[item]))

    response = client.get("/reports/accounts")

    assert response.status_code == 200
    body = response.json()
    assert body == {"items": [item]}


def test_get_account_summary_forwards_account_id_filter(client, fake_user, fake_pool, monkeypatch):
    account_id = str(uuid4())
    get_account_summary_mock = AsyncMock(return_value=[])
    monkeypatch.setattr("app.api.reports.get_account_summary", get_account_summary_mock)

    response = client.get("/reports/accounts", params={"account_id": account_id})

    assert response.status_code == 200
    _, kwargs = get_account_summary_mock.call_args
    assert str(kwargs["account_id"]) == account_id
    assert kwargs["user_id"] == fake_user


def test_get_account_summary_logs_and_returns_generic_500_on_unexpected_error(
    client, fake_user, fake_pool, monkeypatch, caplog
):
    monkeypatch.setattr(
        "app.api.reports.get_account_summary", AsyncMock(side_effect=RuntimeError("connection reset, secret=xyz"))
    )

    with caplog.at_level(logging.ERROR):
        response = client.get("/reports/accounts")

    assert response.status_code == 500
    body = response.json()
    assert body["detail"] == "Something went wrong. Please try again."
    assert "secret=xyz" not in body["detail"]


# --- GET /reports/transfers (ab-150) ------------------------------------


def _transfer_item(**overrides):
    base = {
        "id": str(uuid4()),
        "transfer_reason_name": "Sent to another of my accounts",
        "source_label": "M-Pesa",
        "destination_account_name": "KCB Savings",
        "amount": "500.00",
        "currency": "KES",
        "date": "2026-09-20",
    }
    base.update(overrides)
    return base


def test_get_transfers_requires_auth(client):
    response = client.get("/reports/transfers")
    assert response.status_code == 401


def test_get_transfers_returns_503_when_db_unreachable(client, fake_user):
    response = client.get("/reports/transfers")
    assert response.status_code == 503


def test_get_transfers_success(client, fake_user, fake_pool, monkeypatch):
    item = _transfer_item()
    monkeypatch.setattr("app.api.reports.get_transfers", AsyncMock(return_value=[item]))

    response = client.get("/reports/transfers")

    assert response.status_code == 200
    assert response.json() == {"items": [item]}


def test_get_transfers_forwards_filters_to_the_repository(client, fake_user, fake_pool, monkeypatch):
    account_id = str(uuid4())
    get_transfers_mock = AsyncMock(return_value=[])
    monkeypatch.setattr("app.api.reports.get_transfers", get_transfers_mock)

    response = client.get(
        "/reports/transfers",
        params={"account_id": account_id, "from": "2026-09-01", "to": "2026-10-01"},
    )

    assert response.status_code == 200
    _, kwargs = get_transfers_mock.call_args
    assert str(kwargs["account_id"]) == account_id
    assert str(kwargs["from_date"]) == "2026-09-01"
    assert str(kwargs["to_date"]) == "2026-10-01"
    assert kwargs["user_id"] == fake_user


def test_get_transfers_logs_and_returns_generic_500_on_unexpected_error(
    client, fake_user, fake_pool, monkeypatch, caplog
):
    monkeypatch.setattr(
        "app.api.reports.get_transfers", AsyncMock(side_effect=RuntimeError("connection reset, secret=xyz"))
    )

    with caplog.at_level(logging.ERROR):
        response = client.get("/reports/transfers")

    assert response.status_code == 500
    body = response.json()
    assert body["detail"] == "Something went wrong. Please try again."
    assert "secret=xyz" not in body["detail"]
