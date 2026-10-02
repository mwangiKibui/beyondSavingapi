"""GET /reports/transaction-statement (JSON) and its CSV/PDF exports
(2026-10-02) - mocked at the app.api.reports.get_account/
get_transaction_statement layer, same convention as every other
/reports test file in this suite.
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


def _account():
    return {"id": uuid4(), "nickname": "KCB Salary", "currency": "KES"}


def _statement():
    return {
        "opening_balance": Decimal("1000.00"),
        "rows": [
            {
                "description": "Naivas",
                "date": "2026-09-05",
                "balance": Decimal("800.00"),
                "credit": None,
                "debit": Decimal("200.00"),
                "status": "reconciled",
            },
            {
                "description": "Salary",
                "date": "2026-09-10",
                "balance": Decimal("1800.00"),
                "credit": Decimal("1000.00"),
                "debit": None,
                "status": "unreconciled",
            },
        ],
        "total_credit": Decimal("1000.00"),
        "total_debit": Decimal("200.00"),
        "closing_balance": Decimal("1800.00"),
    }


# --- GET /reports/transaction-statement (JSON) -----------------------------


def test_get_transaction_statement_requires_auth(client):
    response = client.get("/reports/transaction-statement", params={"account_id": str(uuid4())})
    assert response.status_code == 401


def test_get_transaction_statement_returns_503_when_db_unreachable(client, fake_user):
    response = client.get("/reports/transaction-statement", params={"account_id": str(uuid4())})
    assert response.status_code == 503


def test_get_transaction_statement_requires_account_id(client, fake_user, fake_pool):
    response = client.get("/reports/transaction-statement")
    assert response.status_code == 422


def test_get_transaction_statement_returns_404_when_account_not_found(client, fake_user, fake_pool, monkeypatch):
    monkeypatch.setattr("app.api.reports.get_account", AsyncMock(return_value=None))

    response = client.get("/reports/transaction-statement", params={"account_id": str(uuid4())})

    assert response.status_code == 404


def test_get_transaction_statement_success_shape(client, fake_user, fake_pool, monkeypatch):
    monkeypatch.setattr("app.api.reports.get_account", AsyncMock(return_value=_account()))
    monkeypatch.setattr("app.api.reports.get_transaction_statement", AsyncMock(return_value=_statement()))

    response = client.get("/reports/transaction-statement", params={"account_id": str(uuid4())})

    assert response.status_code == 200
    body = response.json()
    assert body["opening_balance"] == "1000.00"
    assert body["closing_balance"] == "1800.00"
    assert body["total_credit"] == "1000.00"
    assert body["total_debit"] == "200.00"
    assert len(body["rows"]) == 2
    assert body["rows"][0]["description"] == "Naivas"
    assert body["rows"][0]["debit"] == "200.00"
    assert body["rows"][0]["credit"] is None


def test_get_transaction_statement_forwards_filters(client, fake_user, fake_pool, monkeypatch):
    account_id = str(uuid4())
    monkeypatch.setattr("app.api.reports.get_account", AsyncMock(return_value=_account()))
    mock = AsyncMock(return_value=_statement())
    monkeypatch.setattr("app.api.reports.get_transaction_statement", mock)

    response = client.get(
        "/reports/transaction-statement",
        params={"account_id": account_id, "from": "2026-09-01", "to": "2026-09-30", "status": "reconciled"},
    )

    assert response.status_code == 200
    _, kwargs = mock.call_args
    assert str(kwargs["account_id"]) == account_id
    assert kwargs["user_id"] == fake_user
    assert str(kwargs["from_date"]) == "2026-09-01"
    assert str(kwargs["to_date"]) == "2026-09-30"
    assert kwargs["status_filter"] == "reconciled"


def test_get_transaction_statement_rejects_an_invalid_status(client, fake_user, fake_pool):
    response = client.get(
        "/reports/transaction-statement", params={"account_id": str(uuid4()), "status": "partial"}
    )
    assert response.status_code == 422


def test_get_transaction_statement_logs_and_returns_generic_500_on_unexpected_error(
    client, fake_user, fake_pool, monkeypatch, caplog
):
    monkeypatch.setattr("app.api.reports.get_account", AsyncMock(return_value=_account()))
    monkeypatch.setattr(
        "app.api.reports.get_transaction_statement",
        AsyncMock(side_effect=RuntimeError("connection reset, secret=xyz")),
    )

    with caplog.at_level(logging.ERROR):
        response = client.get("/reports/transaction-statement", params={"account_id": str(uuid4())})

    assert response.status_code == 500
    body = response.json()
    assert body["detail"] == "Something went wrong. Please try again."
    assert "secret=xyz" not in body["detail"]


# --- CSV ---------------------------------------------------------------


def test_export_transaction_statement_csv_requires_auth(client):
    response = client.get("/reports/transaction-statement/export.csv", params={"account_id": str(uuid4())})
    assert response.status_code == 401


def test_export_transaction_statement_csv_requires_account_id(client, fake_user, fake_pool):
    response = client.get("/reports/transaction-statement/export.csv")
    assert response.status_code == 422


def test_export_transaction_statement_csv_returns_404_when_account_not_found(
    client, fake_user, fake_pool, monkeypatch
):
    monkeypatch.setattr("app.api.reports.get_account", AsyncMock(return_value=None))

    response = client.get("/reports/transaction-statement/export.csv", params={"account_id": str(uuid4())})

    assert response.status_code == 404


def test_export_transaction_statement_csv_success_shape_and_rows(client, fake_user, fake_pool, monkeypatch):
    monkeypatch.setattr("app.api.reports.get_account", AsyncMock(return_value=_account()))
    monkeypatch.setattr("app.api.reports.get_transaction_statement", AsyncMock(return_value=_statement()))

    response = client.get("/reports/transaction-statement/export.csv", params={"account_id": str(uuid4())})

    assert response.status_code == 200
    assert response.headers["content-type"].startswith("text/csv")
    assert response.headers["content-disposition"] == 'attachment; filename="transaction-statement.csv"'

    rows = list(csv.reader(io.StringIO(response.text)))
    assert rows[0] == ["Description", "Balance", "Credit", "Debit"]
    assert rows[1] == ["Balance b/f", "1000.00", "", ""]
    assert rows[2] == ["Naivas", "800.00", "", "200.00"]
    assert rows[3] == ["Salary", "1800.00", "1000.00", ""]
    assert rows[4] == ["Total", "1800.00", "1000.00", "200.00"]


def test_export_transaction_statement_csv_omits_balance_bf_when_unknown(client, fake_user, fake_pool, monkeypatch):
    statement = _statement()
    statement["opening_balance"] = None
    monkeypatch.setattr("app.api.reports.get_account", AsyncMock(return_value=_account()))
    monkeypatch.setattr("app.api.reports.get_transaction_statement", AsyncMock(return_value=statement))

    response = client.get("/reports/transaction-statement/export.csv", params={"account_id": str(uuid4())})

    rows = list(csv.reader(io.StringIO(response.text)))
    assert rows[1] == ["Naivas", "800.00", "", "200.00"]


def test_export_transaction_statement_csv_logs_and_returns_generic_500_on_unexpected_error(
    client, fake_user, fake_pool, monkeypatch, caplog
):
    monkeypatch.setattr("app.api.reports.get_account", AsyncMock(return_value=_account()))
    monkeypatch.setattr(
        "app.api.reports.get_transaction_statement",
        AsyncMock(side_effect=RuntimeError("connection reset, secret=xyz")),
    )

    with caplog.at_level(logging.ERROR):
        response = client.get("/reports/transaction-statement/export.csv", params={"account_id": str(uuid4())})

    assert response.status_code == 500
    body = response.json()
    assert body["detail"] == "Something went wrong. Please try again."
    assert "secret=xyz" not in body["detail"]


# --- PDF ---------------------------------------------------------------


def test_export_transaction_statement_pdf_requires_auth(client):
    response = client.get("/reports/transaction-statement/export.pdf", params={"account_id": str(uuid4())})
    assert response.status_code == 401


def test_export_transaction_statement_pdf_returns_404_when_account_not_found(
    client, fake_user, fake_pool, monkeypatch
):
    monkeypatch.setattr("app.api.reports.get_account", AsyncMock(return_value=None))

    response = client.get("/reports/transaction-statement/export.pdf", params={"account_id": str(uuid4())})

    assert response.status_code == 404


def test_export_transaction_statement_pdf_success_returns_valid_pdf_bytes(client, fake_user, fake_pool, monkeypatch):
    monkeypatch.setattr("app.api.reports.get_account", AsyncMock(return_value=_account()))
    monkeypatch.setattr("app.api.reports.get_transaction_statement", AsyncMock(return_value=_statement()))

    response = client.get("/reports/transaction-statement/export.pdf", params={"account_id": str(uuid4())})

    assert response.status_code == 200
    assert response.headers["content-type"].startswith("application/pdf")
    assert response.headers["content-disposition"] == 'attachment; filename="transaction-statement.pdf"'
    assert response.content.startswith(b"%PDF")


def test_export_transaction_statement_pdf_with_no_rows_still_renders(client, fake_user, fake_pool, monkeypatch):
    empty = {"opening_balance": None, "rows": [], "total_credit": Decimal("0.00"), "total_debit": Decimal("0.00"), "closing_balance": None}
    monkeypatch.setattr("app.api.reports.get_account", AsyncMock(return_value=_account()))
    monkeypatch.setattr("app.api.reports.get_transaction_statement", AsyncMock(return_value=empty))

    response = client.get("/reports/transaction-statement/export.pdf", params={"account_id": str(uuid4())})

    assert response.status_code == 200
    assert response.content.startswith(b"%PDF")


def test_export_transaction_statement_pdf_logs_and_returns_generic_500_on_unexpected_error(
    client, fake_user, fake_pool, monkeypatch, caplog
):
    monkeypatch.setattr("app.api.reports.get_account", AsyncMock(return_value=_account()))
    monkeypatch.setattr(
        "app.api.reports.get_transaction_statement",
        AsyncMock(side_effect=RuntimeError("connection reset, secret=xyz")),
    )

    with caplog.at_level(logging.ERROR):
        response = client.get("/reports/transaction-statement/export.pdf", params={"account_id": str(uuid4())})

    assert response.status_code == 500
    body = response.json()
    assert body["detail"] == "Something went wrong. Please try again."
    assert "secret=xyz" not in body["detail"]
