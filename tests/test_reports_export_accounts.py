"""GET /reports/accounts/export.csv|.pdf (ab-152) - CSV/PDF exports of
the existing Account Summary report (GET /reports/accounts), mocked at
the app.api.reports.get_account_summary layer same as
test_reports_get.py's own GET /reports/accounts tests.
"""

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


def _items():
    return [
        {
            "account_id": str(uuid4()),
            "account_nickname": "KCB Salary",
            "money_in": Decimal("12345.00"),
            "money_out": Decimal("6789.00"),
        },
        {
            "account_id": str(uuid4()),
            "account_nickname": "M-Pesa",
            "money_in": Decimal("500.00"),
            "money_out": Decimal("200.00"),
        },
    ]


# --- CSV -------------------------------------------------------------------


def test_export_account_summary_csv_requires_auth(client):
    response = client.get("/reports/accounts/export.csv")
    assert response.status_code == 401


def test_export_account_summary_csv_returns_503_when_db_unreachable(client, fake_user):
    response = client.get("/reports/accounts/export.csv")
    assert response.status_code == 503


def test_export_account_summary_csv_success_shape_and_rows(client, fake_user, fake_pool, monkeypatch):
    monkeypatch.setattr("app.api.reports.get_account_summary", AsyncMock(return_value=_items()))

    response = client.get("/reports/accounts/export.csv")

    assert response.status_code == 200
    assert response.headers["content-type"].startswith("text/csv")
    assert response.headers["content-disposition"] == 'attachment; filename="account-summary.csv"'

    rows = list(csv.reader(io.StringIO(response.text)))
    assert rows[0] == ["Account", "Money In", "Money Out"]
    assert rows[1] == ["KCB Salary", "12345.00", "6789.00"]
    assert rows[2] == ["M-Pesa", "500.00", "200.00"]


def test_export_account_summary_csv_forwards_account_id_filter(client, fake_user, fake_pool, monkeypatch):
    account_id = str(uuid4())
    mock = AsyncMock(return_value=[])
    monkeypatch.setattr("app.api.reports.get_account_summary", mock)

    response = client.get("/reports/accounts/export.csv", params={"account_id": account_id})

    assert response.status_code == 200
    _, kwargs = mock.call_args
    assert str(kwargs["account_id"]) == account_id
    assert kwargs["user_id"] == fake_user


def test_export_account_summary_csv_logs_and_returns_generic_500_on_unexpected_error(
    client, fake_user, fake_pool, monkeypatch, caplog
):
    monkeypatch.setattr(
        "app.api.reports.get_account_summary", AsyncMock(side_effect=RuntimeError("connection reset, secret=xyz"))
    )

    with caplog.at_level(logging.ERROR):
        response = client.get("/reports/accounts/export.csv")

    assert response.status_code == 500
    body = response.json()
    assert body["detail"] == "Something went wrong. Please try again."
    assert "secret=xyz" not in body["detail"]


# --- PDF -------------------------------------------------------------------


def test_export_account_summary_pdf_requires_auth(client):
    response = client.get("/reports/accounts/export.pdf")
    assert response.status_code == 401


def test_export_account_summary_pdf_returns_503_when_db_unreachable(client, fake_user):
    response = client.get("/reports/accounts/export.pdf")
    assert response.status_code == 503


def test_export_account_summary_pdf_success_returns_valid_pdf_bytes(client, fake_user, fake_pool, monkeypatch):
    monkeypatch.setattr("app.api.reports.get_account_summary", AsyncMock(return_value=_items()))

    response = client.get("/reports/accounts/export.pdf")

    assert response.status_code == 200
    assert response.headers["content-type"].startswith("application/pdf")
    assert response.headers["content-disposition"] == 'attachment; filename="account-summary.pdf"'
    assert response.content.startswith(b"%PDF")


def test_export_account_summary_pdf_with_no_matching_rows_still_renders(client, fake_user, fake_pool, monkeypatch):
    monkeypatch.setattr("app.api.reports.get_account_summary", AsyncMock(return_value=[]))

    response = client.get("/reports/accounts/export.pdf")

    assert response.status_code == 200
    assert response.content.startswith(b"%PDF")


def test_export_account_summary_pdf_logs_and_returns_generic_500_on_unexpected_error(
    client, fake_user, fake_pool, monkeypatch, caplog
):
    monkeypatch.setattr(
        "app.api.reports.get_account_summary", AsyncMock(side_effect=RuntimeError("connection reset, secret=xyz"))
    )

    with caplog.at_level(logging.ERROR):
        response = client.get("/reports/accounts/export.pdf")

    assert response.status_code == 500
    body = response.json()
    assert body["detail"] == "Something went wrong. Please try again."
    assert "secret=xyz" not in body["detail"]
