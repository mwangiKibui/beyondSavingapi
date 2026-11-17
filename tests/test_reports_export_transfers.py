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
            "date": "2026-09-20",
            "from_label": "M-Pesa",
            "from_account_id": uuid4(),
            "to_label": "KCB Savings",
            "to_account_id": uuid4(),
            "reason": "Sent to your KCB account",
            "amount": Decimal("500.00"),
            "currency": "KES",
        },
        {
            "id": str(uuid4()),
            "date": "2026-09-18",
            "from_label": "NCBA",
            "from_account_id": None,
            "to_label": "KCB Salary",
            "to_account_id": uuid4(),
            "reason": None,
            "amount": Decimal("1000.00"),
            "currency": "KES",
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
    assert response.headers["content-disposition"] == 'attachment; filename="funds-transfers.csv"'

    rows = list(csv.reader(io.StringIO(response.text)))
    assert rows[0] == ["Date", "From", "To", "Reason", "Amount", "Currency"]
    assert rows[1] == ["2026-09-20", "M-Pesa", "KCB Savings", "Sent to your KCB account", "500.00", "KES"]
    # No reason falls back to an em dash.
    assert rows[2] == ["2026-09-18", "NCBA", "KCB Salary", "—", "1000.00", "KES"]


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
    assert response.headers["content-disposition"] == 'attachment; filename="funds-transfers.pdf"'
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
