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
