import logging
from datetime import date
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


def _fetch_report_rows_payload():
    """fetch_report_rows()-shaped rows for one categorized transaction -
    exercises the real build_report/build_report_pdf pipeline (not
    mocked) so the test confirms actual PDF bytes come out, not just that
    some mock returned something."""
    txn_id = uuid4()
    category_id = uuid4()
    return [
        {
            "transaction_id": txn_id,
            "txn_date": date(2026, 10, 1),
            "txn_amount": Decimal("500.00"),
            "txn_currency": "KES",
            "direction": "out",
            "counterparty": "Naivas",
            "description": "Weekly shopping",
            "account_nickname": "KCB Salary",
            "allocation_id": uuid4(),
            "category_id": category_id,
            "category_name": "Groceries",
            "category_type": "expense",
            "transfer_reason_id": None,
            "alloc_amount": Decimal("500.00"),
            "alloc_currency": "KES",
        }
    ]


def test_export_pdf_requires_auth(client):
    response = client.get("/reports/export.pdf")
    assert response.status_code == 401


def test_export_pdf_returns_503_when_db_unreachable(client, fake_user):
    response = client.get("/reports/export.pdf")
    assert response.status_code == 503


def test_export_pdf_success_returns_valid_pdf_bytes(client, fake_user, fake_pool, monkeypatch):
    monkeypatch.setattr("app.api.reports.resolve_report_currency", AsyncMock(return_value="KES"))
    monkeypatch.setattr(
        "app.api.reports.fetch_report_rows", AsyncMock(return_value=_fetch_report_rows_payload())
    )

    response = client.get("/reports/export.pdf")

    assert response.status_code == 200
    assert response.headers["content-type"].startswith("application/pdf")
    assert response.headers["content-disposition"] == 'attachment; filename="report.pdf"'
    assert response.content.startswith(b"%PDF")
    assert len(response.content) > 0


def test_export_pdf_with_no_matching_rows_still_renders(client, fake_user, fake_pool, monkeypatch):
    monkeypatch.setattr("app.api.reports.resolve_report_currency", AsyncMock(return_value="KES"))
    monkeypatch.setattr("app.api.reports.fetch_report_rows", AsyncMock(return_value=[]))

    response = client.get("/reports/export.pdf")

    assert response.status_code == 200
    assert response.content.startswith(b"%PDF")


def test_export_pdf_returns_404_when_account_not_found(client, fake_user, fake_pool, monkeypatch):
    monkeypatch.setattr("app.api.reports.get_account", AsyncMock(return_value=None))

    response = client.get("/reports/export.pdf", params={"account_id": str(uuid4())})

    assert response.status_code == 404


def test_export_pdf_logs_and_returns_generic_500_on_unexpected_error(client, fake_user, fake_pool, monkeypatch, caplog):
    monkeypatch.setattr("app.api.reports.resolve_report_currency", AsyncMock(return_value="KES"))
    monkeypatch.setattr(
        "app.api.reports.fetch_report_rows", AsyncMock(side_effect=RuntimeError("connection reset, secret=xyz"))
    )

    with caplog.at_level(logging.ERROR):
        response = client.get("/reports/export.pdf")

    assert response.status_code == 500
    body = response.json()
    assert body["detail"] == "Something went wrong. Please try again."
    assert "secret=xyz" not in body["detail"]
