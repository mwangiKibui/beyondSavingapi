"""GET /reports/transfers/export.csv|.pdf (ab-152) - CSV/PDF exports of
the existing Transfers report (GET /reports/transfers), mocked at the
app.api.reports.get_transfers layer same as test_reports_get.py's own
GET /reports/transfers tests.
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
            "id": str(uuid4()),
            "transfer_reason_name": "Sent to another of my accounts",
            "source_label": "M-Pesa",
            "destination_account_name": "KCB Savings",
            "amount": Decimal("500.00"),
            "currency": "KES",
            "date": "2026-09-20",
        },
        {
            "id": str(uuid4()),
            "transfer_reason_name": None,
            "source_label": "Cash deposit from a friend",
            "destination_account_name": "KCB Salary",
            "amount": Decimal("1000.00"),
            "currency": "KES",
            "date": "2026-09-18",
        },
    ]


# --- CSV -------------------------------------------------------------------


def test_export_transfers_csv_requires_auth(client):
    response = client.get("/reports/transfers/export.csv")
    assert response.status_code == 401


def test_export_transfers_csv_returns_503_when_db_unreachable(client, fake_user):
    response = client.get("/reports/transfers/export.csv")
    assert response.status_code == 503


def test_export_transfers_csv_success_shape_and_rows(client, fake_user, fake_pool, monkeypatch):
    monkeypatch.setattr("app.api.reports.get_transfers", AsyncMock(return_value=_items()))

    response = client.get("/reports/transfers/export.csv")

    assert response.status_code == 200
    assert response.headers["content-type"].startswith("text/csv")
    assert response.headers["content-disposition"] == 'attachment; filename="transfers.csv"'

    rows = list(csv.reader(io.StringIO(response.text)))
    assert rows[0] == ["Transfer Nature", "Source Account", "Destination Account", "Amount", "Date"]
    assert rows[1] == ["Sent to another of my accounts", "M-Pesa", "KCB Savings", "500.00", "2026-09-20"]
    # No transfer_reason_name falls back to the em dash placeholder, same
    # convention as _resolve_transfer_source_label's own "—" fallback.
    assert rows[2] == ["—", "Cash deposit from a friend", "KCB Salary", "1000.00", "2026-09-18"]


def test_export_transfers_csv_forwards_filters(client, fake_user, fake_pool, monkeypatch):
    account_id = str(uuid4())
    mock = AsyncMock(return_value=[])
    monkeypatch.setattr("app.api.reports.get_transfers", mock)

    response = client.get(
        "/reports/transfers/export.csv",
        params={"account_id": account_id, "from": "2026-09-01", "to": "2026-09-30"},
    )

    assert response.status_code == 200
    _, kwargs = mock.call_args
    assert str(kwargs["account_id"]) == account_id
    assert str(kwargs["from_date"]) == "2026-09-01"
    assert str(kwargs["to_date"]) == "2026-09-30"
    assert kwargs["user_id"] == fake_user


def test_export_transfers_csv_logs_and_returns_generic_500_on_unexpected_error(
    client, fake_user, fake_pool, monkeypatch, caplog
):
    monkeypatch.setattr(
        "app.api.reports.get_transfers", AsyncMock(side_effect=RuntimeError("connection reset, secret=xyz"))
    )

    with caplog.at_level(logging.ERROR):
        response = client.get("/reports/transfers/export.csv")

    assert response.status_code == 500
    body = response.json()
    assert body["detail"] == "Something went wrong. Please try again."
    assert "secret=xyz" not in body["detail"]


# --- PDF -------------------------------------------------------------------


def test_export_transfers_pdf_requires_auth(client):
    response = client.get("/reports/transfers/export.pdf")
    assert response.status_code == 401


def test_export_transfers_pdf_returns_503_when_db_unreachable(client, fake_user):
    response = client.get("/reports/transfers/export.pdf")
    assert response.status_code == 503


def test_export_transfers_pdf_success_returns_valid_pdf_bytes(client, fake_user, fake_pool, monkeypatch):
    monkeypatch.setattr("app.api.reports.get_transfers", AsyncMock(return_value=_items()))

    response = client.get("/reports/transfers/export.pdf")

    assert response.status_code == 200
    assert response.headers["content-type"].startswith("application/pdf")
    assert response.headers["content-disposition"] == 'attachment; filename="transfers.pdf"'
    assert response.content.startswith(b"%PDF")


def test_export_transfers_pdf_with_no_matching_rows_still_renders(client, fake_user, fake_pool, monkeypatch):
    monkeypatch.setattr("app.api.reports.get_transfers", AsyncMock(return_value=[]))

    response = client.get("/reports/transfers/export.pdf")

    assert response.status_code == 200
    assert response.content.startswith(b"%PDF")


def test_export_transfers_pdf_logs_and_returns_generic_500_on_unexpected_error(
    client, fake_user, fake_pool, monkeypatch, caplog
):
    monkeypatch.setattr(
        "app.api.reports.get_transfers", AsyncMock(side_effect=RuntimeError("connection reset, secret=xyz"))
    )

    with caplog.at_level(logging.ERROR):
        response = client.get("/reports/transfers/export.pdf")

    assert response.status_code == 500
    body = response.json()
    assert body["detail"] == "Something went wrong. Please try again."
    assert "secret=xyz" not in body["detail"]
