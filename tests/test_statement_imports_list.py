import logging
from unittest.mock import AsyncMock
from uuid import uuid4

import pytest
from fastapi.testclient import TestClient

from app.core.db import get_pool
from app.core.security import get_current_user_id
from app.main import app

SAMPLE_ITEM = {
    "id": str(uuid4()),
    "account_id": str(uuid4()),
    "account_nickname": "My Mpesa",
    "file_name": "statement.pdf",
    "status": "pending",
    "period_start": None,
    "period_end": None,
    "row_count": None,
    "error_detail": None,
    "created_at": "2026-09-17T00:00:00Z",
}


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


def test_list_statement_imports_requires_auth(client):
    response = client.get("/statement-imports")
    assert response.status_code == 401


def test_list_statement_imports_returns_503_when_db_unreachable(client, fake_user):
    response = client.get("/statement-imports")
    assert response.status_code == 503


def test_list_statement_imports_defaults(client, fake_user, fake_pool, monkeypatch):
    mock_list = AsyncMock(return_value=([SAMPLE_ITEM], 1))
    monkeypatch.setattr("app.api.statement_imports.list_statement_imports", mock_list)

    response = client.get("/statement-imports")

    assert response.status_code == 200
    body = response.json()
    assert body == {"items": [SAMPLE_ITEM], "total": 1, "page": 1, "page_size": 10}

    _, kwargs = mock_list.call_args
    assert kwargs["user_id"] == fake_user
    assert kwargs["page"] == 1
    assert kwargs["page_size"] == 10
    assert kwargs["search"] is None
    assert kwargs["status"] is None
    assert kwargs["account_id"] is None
    assert kwargs["sort_dir"] == "desc"


def test_list_statement_imports_forwards_all_query_params(client, fake_user, fake_pool, monkeypatch):
    mock_list = AsyncMock(return_value=([], 0))
    monkeypatch.setattr("app.api.statement_imports.list_statement_imports", mock_list)
    account_id = uuid4()

    response = client.get(
        "/statement-imports",
        params={
            "page": 2,
            "page_size": 20,
            "search": "statement",
            "status": "failed",
            "account_id": str(account_id),
            "sort_dir": "asc",
        },
    )

    assert response.status_code == 200
    _, kwargs = mock_list.call_args
    assert kwargs["page"] == 2
    assert kwargs["page_size"] == 20
    assert kwargs["search"] == "statement"
    assert kwargs["status"] == "failed"
    assert kwargs["account_id"] == account_id
    assert kwargs["sort_dir"] == "asc"


@pytest.mark.parametrize(
    "params",
    [
        {"page_size": 15},
        {"status": "unknown"},
        {"sort_dir": "upwards"},
        {"account_id": "not-a-uuid"},
        {"page": 0},
    ],
)
def test_list_statement_imports_rejects_invalid_query_params(client, fake_user, params):
    response = client.get("/statement-imports", params=params)
    assert response.status_code == 422


def test_list_statement_imports_logs_and_returns_generic_500_on_unexpected_error(
    client, fake_user, fake_pool, monkeypatch, caplog
):
    mock_list = AsyncMock(side_effect=RuntimeError("connection reset, secret=xyz"))
    monkeypatch.setattr("app.api.statement_imports.list_statement_imports", mock_list)

    with caplog.at_level(logging.ERROR):
        response = client.get("/statement-imports")

    assert response.status_code == 500
    body = response.json()
    assert body["detail"] == "Something went wrong. Please try again."
    assert "secret=xyz" not in body["detail"]

    error_records = [r for r in caplog.records if r.levelno == logging.ERROR]
    assert len(error_records) == 1
    assert error_records[0].exc_info is not None
