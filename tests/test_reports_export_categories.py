"""GET /reports/categories/export.csv|.pdf (ab-152) - CSV/PDF exports
shared by the Expenses and Income report views, mocked at the
app.api.reports.get_category_summary layer. get_category_summary's own
reuse of build_report's by-category aggregation is covered at the
repository level in tests/test_reports_aggregation.py - this file only
exercises the HTTP layer.
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
            "category_id": str(uuid4()),
            "category_name": "Groceries",
            "category_type": "expense",
            "total": Decimal("3000.00"),
        },
        {
            "category_id": str(uuid4()),
            "category_name": "Transport",
            "category_type": "expense",
            "total": Decimal("1200.00"),
        },
    ]


# --- CSV -------------------------------------------------------------------


def test_export_category_summary_csv_requires_auth(client):
    response = client.get("/reports/categories/export.csv", params={"category_type": "expense"})
    assert response.status_code == 401


def test_export_category_summary_csv_returns_503_when_db_unreachable(client, fake_user):
    response = client.get("/reports/categories/export.csv", params={"category_type": "expense"})
    assert response.status_code == 503


def test_export_category_summary_csv_requires_category_type(client, fake_user, fake_pool):
    response = client.get("/reports/categories/export.csv")
    assert response.status_code == 422


def test_export_category_summary_csv_rejects_an_invalid_category_type(client, fake_user, fake_pool):
    response = client.get("/reports/categories/export.csv", params={"category_type": "transfer"})
    assert response.status_code == 422


def test_export_category_summary_csv_success_shape_and_rows_for_expense(client, fake_user, fake_pool, monkeypatch):
    monkeypatch.setattr("app.api.reports.resolve_report_currency", AsyncMock(return_value="KES"))
    monkeypatch.setattr("app.api.reports.get_category_summary", AsyncMock(return_value=_items()))

    response = client.get("/reports/categories/export.csv", params={"category_type": "expense"})

    assert response.status_code == 200
    assert response.headers["content-type"].startswith("text/csv")
    assert response.headers["content-disposition"] == 'attachment; filename="expense-summary.csv"'

    rows = list(csv.reader(io.StringIO(response.text)))
    assert rows[0] == ["Category", "Amount"]
    assert rows[1] == ["Groceries", "3000.00"]
    assert rows[2] == ["Transport", "1200.00"]


def test_export_category_summary_csv_uses_income_filename_for_income_type(client, fake_user, fake_pool, monkeypatch):
    monkeypatch.setattr("app.api.reports.resolve_report_currency", AsyncMock(return_value="KES"))
    monkeypatch.setattr("app.api.reports.get_category_summary", AsyncMock(return_value=[]))

    response = client.get("/reports/categories/export.csv", params={"category_type": "income"})

    assert response.status_code == 200
    assert response.headers["content-disposition"] == 'attachment; filename="income-summary.csv"'


def test_export_category_summary_csv_forwards_category_type_and_from_to(client, fake_user, fake_pool, monkeypatch):
    monkeypatch.setattr("app.api.reports.resolve_report_currency", AsyncMock(return_value="KES"))
    mock = AsyncMock(return_value=[])
    monkeypatch.setattr("app.api.reports.get_category_summary", mock)

    response = client.get(
        "/reports/categories/export.csv",
        params={"category_type": "income", "from": "2026-09-01", "to": "2026-09-30"},
    )

    assert response.status_code == 200
    args, kwargs = mock.call_args
    filters_arg = args[1]
    assert str(filters_arg.from_date) == "2026-09-01"
    assert str(filters_arg.to_date) == "2026-09-30"
    assert kwargs["category_type"] == "income"


def test_export_category_summary_csv_logs_and_returns_generic_500_on_unexpected_error(
    client, fake_user, fake_pool, monkeypatch, caplog
):
    monkeypatch.setattr("app.api.reports.resolve_report_currency", AsyncMock(return_value="KES"))
    monkeypatch.setattr(
        "app.api.reports.get_category_summary", AsyncMock(side_effect=RuntimeError("connection reset, secret=xyz"))
    )

    with caplog.at_level(logging.ERROR):
        response = client.get("/reports/categories/export.csv", params={"category_type": "expense"})

    assert response.status_code == 500
    body = response.json()
    assert body["detail"] == "Something went wrong. Please try again."
    assert "secret=xyz" not in body["detail"]


# --- PDF -------------------------------------------------------------------


def test_export_category_summary_pdf_requires_auth(client):
    response = client.get("/reports/categories/export.pdf", params={"category_type": "expense"})
    assert response.status_code == 401


def test_export_category_summary_pdf_returns_503_when_db_unreachable(client, fake_user):
    response = client.get("/reports/categories/export.pdf", params={"category_type": "expense"})
    assert response.status_code == 503


def test_export_category_summary_pdf_requires_category_type(client, fake_user, fake_pool):
    response = client.get("/reports/categories/export.pdf")
    assert response.status_code == 422


def test_export_category_summary_pdf_success_returns_valid_pdf_bytes(client, fake_user, fake_pool, monkeypatch):
    monkeypatch.setattr("app.api.reports.resolve_report_currency", AsyncMock(return_value="KES"))
    monkeypatch.setattr("app.api.reports.get_category_summary", AsyncMock(return_value=_items()))

    response = client.get("/reports/categories/export.pdf", params={"category_type": "expense"})

    assert response.status_code == 200
    assert response.headers["content-type"].startswith("application/pdf")
    assert response.headers["content-disposition"] == 'attachment; filename="expense-summary.pdf"'
    assert response.content.startswith(b"%PDF")


def test_export_category_summary_pdf_uses_income_filename_for_income_type(client, fake_user, fake_pool, monkeypatch):
    monkeypatch.setattr("app.api.reports.resolve_report_currency", AsyncMock(return_value="KES"))
    monkeypatch.setattr("app.api.reports.get_category_summary", AsyncMock(return_value=[]))

    response = client.get("/reports/categories/export.pdf", params={"category_type": "income"})

    assert response.status_code == 200
    assert response.headers["content-disposition"] == 'attachment; filename="income-summary.pdf"'
    assert response.content.startswith(b"%PDF")


def test_export_category_summary_pdf_logs_and_returns_generic_500_on_unexpected_error(
    client, fake_user, fake_pool, monkeypatch, caplog
):
    monkeypatch.setattr("app.api.reports.resolve_report_currency", AsyncMock(return_value="KES"))
    monkeypatch.setattr(
        "app.api.reports.get_category_summary", AsyncMock(side_effect=RuntimeError("connection reset, secret=xyz"))
    )

    with caplog.at_level(logging.ERROR):
        response = client.get("/reports/categories/export.pdf", params={"category_type": "expense"})

    assert response.status_code == 500
    body = response.json()
    assert body["detail"] == "Something went wrong. Please try again."
    assert "secret=xyz" not in body["detail"]
