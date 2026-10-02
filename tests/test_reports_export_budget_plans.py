"""GET /reports/budget-plans/export.csv|.pdf (ab-152) - CSV/PDF exports
of budget plans whose window overlaps the given from/to range, mocked at
the app.api.reports.get_budget_plans_report layer, same convention as
every other report export test in this suite.

The window-overlap filter's own correctness (a plan that starts before
`from` but ends inside the range is still included) is covered at the
repository level in tests/test_reports_aggregation.py
(filter_budget_plans_by_window) - this file only exercises the HTTP
layer (auth, 503, Content-Type/Content-Disposition, row/column shape,
filter forwarding).
"""

import csv
import io
import logging
from datetime import datetime, timezone
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


def _plans():
    return [
        {
            "id": str(uuid4()),
            "name": "This month",
            "starts_at": datetime(2026, 9, 1, tzinfo=timezone.utc),
            "ends_at": datetime(2026, 9, 30, tzinfo=timezone.utc),
            "total_cap": Decimal("5000.00"),
            "currency": "KES",
            "money_in": Decimal("15000.00"),
            "money_out": Decimal("4200.00"),
        },
        {
            "id": str(uuid4()),
            "name": None,
            "starts_at": datetime(2026, 8, 15, tzinfo=timezone.utc),
            "ends_at": datetime(2026, 9, 10, tzinfo=timezone.utc),
            "total_cap": None,
            "currency": "KES",
            "money_in": Decimal("0.00"),
            "money_out": Decimal("0.00"),
        },
    ]


# --- CSV -------------------------------------------------------------------


def test_export_budget_plans_csv_requires_auth(client):
    response = client.get("/reports/budget-plans/export.csv")
    assert response.status_code == 401


def test_export_budget_plans_csv_returns_503_when_db_unreachable(client, fake_user):
    response = client.get("/reports/budget-plans/export.csv")
    assert response.status_code == 503


def test_export_budget_plans_csv_success_shape_and_rows(client, fake_user, fake_pool, monkeypatch):
    monkeypatch.setattr("app.api.reports.get_budget_plans_report", AsyncMock(return_value=_plans()))

    response = client.get("/reports/budget-plans/export.csv")

    assert response.status_code == 200
    assert response.headers["content-type"].startswith("text/csv")
    assert response.headers["content-disposition"] == 'attachment; filename="budget-plans.csv"'

    rows = list(csv.reader(io.StringIO(response.text)))
    assert rows[0] == ["Plan Name", "Window (start - end)", "Total Cap", "Money In", "Money Out"]
    assert rows[1] == ["This month", "2026-09-01 - 2026-09-30", "5000.00", "15000.00", "4200.00"]
    # A plan with no name and no total_cap falls back to the em dash
    # placeholder, same convention as build_csv_rows' own "—" for an
    # empty category column.
    assert rows[2] == ["—", "2026-08-15 - 2026-09-10", "—", "0.00", "0.00"]


def test_export_budget_plans_csv_forwards_from_to_filters(client, fake_user, fake_pool, monkeypatch):
    mock = AsyncMock(return_value=[])
    monkeypatch.setattr("app.api.reports.get_budget_plans_report", mock)

    response = client.get(
        "/reports/budget-plans/export.csv", params={"from": "2026-09-01", "to": "2026-09-30"}
    )

    assert response.status_code == 200
    _, kwargs = mock.call_args
    assert str(kwargs["from_date"]) == "2026-09-01"
    assert str(kwargs["to_date"]) == "2026-09-30"
    assert kwargs["user_id"] == fake_user


def test_export_budget_plans_csv_logs_and_returns_generic_500_on_unexpected_error(
    client, fake_user, fake_pool, monkeypatch, caplog
):
    monkeypatch.setattr(
        "app.api.reports.get_budget_plans_report", AsyncMock(side_effect=RuntimeError("connection reset, secret=xyz"))
    )

    with caplog.at_level(logging.ERROR):
        response = client.get("/reports/budget-plans/export.csv")

    assert response.status_code == 500
    body = response.json()
    assert body["detail"] == "Something went wrong. Please try again."
    assert "secret=xyz" not in body["detail"]


# --- PDF -------------------------------------------------------------------


def test_export_budget_plans_pdf_requires_auth(client):
    response = client.get("/reports/budget-plans/export.pdf")
    assert response.status_code == 401


def test_export_budget_plans_pdf_returns_503_when_db_unreachable(client, fake_user):
    response = client.get("/reports/budget-plans/export.pdf")
    assert response.status_code == 503


def test_export_budget_plans_pdf_success_returns_valid_pdf_bytes(client, fake_user, fake_pool, monkeypatch):
    monkeypatch.setattr("app.api.reports.get_budget_plans_report", AsyncMock(return_value=_plans()))

    response = client.get("/reports/budget-plans/export.pdf")

    assert response.status_code == 200
    assert response.headers["content-type"].startswith("application/pdf")
    assert response.headers["content-disposition"] == 'attachment; filename="budget-plans.pdf"'
    assert response.content.startswith(b"%PDF")


def test_export_budget_plans_pdf_with_no_matching_rows_still_renders(client, fake_user, fake_pool, monkeypatch):
    monkeypatch.setattr("app.api.reports.get_budget_plans_report", AsyncMock(return_value=[]))

    response = client.get("/reports/budget-plans/export.pdf")

    assert response.status_code == 200
    assert response.content.startswith(b"%PDF")


def test_export_budget_plans_pdf_logs_and_returns_generic_500_on_unexpected_error(
    client, fake_user, fake_pool, monkeypatch, caplog
):
    monkeypatch.setattr(
        "app.api.reports.get_budget_plans_report", AsyncMock(side_effect=RuntimeError("connection reset, secret=xyz"))
    )

    with caplog.at_level(logging.ERROR):
        response = client.get("/reports/budget-plans/export.pdf")

    assert response.status_code == 500
    body = response.json()
    assert body["detail"] == "Something went wrong. Please try again."
    assert "secret=xyz" not in body["detail"]
