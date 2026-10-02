import csv
import io
import logging
from decimal import Decimal
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


def test_export_csv_requires_auth(client):
    response = client.get("/reports/export.csv")
    assert response.status_code == 401


def test_export_csv_returns_503_when_db_unreachable(client, fake_user):
    response = client.get("/reports/export.csv")
    assert response.status_code == 503


def test_export_csv_success_shape_and_rows(client, fake_user, fake_pool, monkeypatch):
    monkeypatch.setattr("app.api.reports.resolve_report_currency", AsyncMock(return_value="KES"))
    csv_rows = [
        {
            "txn_date": "2026-10-02",
            "account_nickname": "KCB Salary",
            "direction": "out",
            "amount": Decimal("150.00"),
            "currency": "KES",
            "categories": "Groceries, Misc",
            "counterparty": "Naivas",
            "description": "Weekly shopping",
        },
        {
            "txn_date": "2026-10-01",
            "account_nickname": "M-Pesa",
            "direction": "in",
            "amount": Decimal("5000.00"),
            "currency": "KES",
            "categories": "—",
            "counterparty": "",
            "description": "",
        },
    ]
    monkeypatch.setattr("app.api.reports.get_report_csv_rows", AsyncMock(return_value=csv_rows))

    response = client.get("/reports/export.csv")

    assert response.status_code == 200
    assert response.headers["content-type"].startswith("text/csv")
    assert response.headers["content-disposition"] == 'attachment; filename="report.csv"'

    rows = list(csv.reader(io.StringIO(response.text)))
    assert rows[0] == ["Date", "Account", "Direction", "Amount", "Currency", "Categories", "Counterparty", "Description"]
    assert rows[1] == ["2026-10-02", "KCB Salary", "out", "150.00", "KES", "Groceries, Misc", "Naivas", "Weekly shopping"]
    assert rows[2] == ["2026-10-01", "M-Pesa", "in", "5000.00", "KES", "—", "", ""]


def test_export_csv_returns_404_when_account_not_found(client, fake_user, fake_pool, monkeypatch):
    monkeypatch.setattr("app.api.reports.get_account", AsyncMock(return_value=None))

    response = client.get("/reports/export.csv", params={"account_id": str(uuid4())})

    assert response.status_code == 404


def test_export_csv_logs_and_returns_generic_500_on_unexpected_error(client, fake_user, fake_pool, monkeypatch, caplog):
    monkeypatch.setattr("app.api.reports.resolve_report_currency", AsyncMock(return_value="KES"))
    monkeypatch.setattr(
        "app.api.reports.get_report_csv_rows", AsyncMock(side_effect=RuntimeError("connection reset, secret=xyz"))
    )

    with caplog.at_level(logging.ERROR):
        response = client.get("/reports/export.csv")

    assert response.status_code == 500
    body = response.json()
    assert body["detail"] == "Something went wrong. Please try again."
    assert "secret=xyz" not in body["detail"]
