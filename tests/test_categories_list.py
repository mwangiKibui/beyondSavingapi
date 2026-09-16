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
    "name": "Groceries",
    "type": "expense",
    "is_default": True,
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


def test_list_categories_requires_auth(client):
    response = client.get("/categories", params={"type": "expense"})
    assert response.status_code == 401


def test_list_categories_requires_type(client, fake_user):
    response = client.get("/categories")
    assert response.status_code == 422


def test_list_categories_returns_503_when_db_unreachable(client, fake_user):
    response = client.get("/categories", params={"type": "expense"})
    assert response.status_code == 503


def test_list_categories_defaults(client, fake_user, fake_pool, monkeypatch):
    mock_list_categories = AsyncMock(return_value=([SAMPLE_ITEM], 1))
    monkeypatch.setattr("app.api.categories.list_categories", mock_list_categories)

    response = client.get("/categories", params={"type": "expense"})

    assert response.status_code == 200
    body = response.json()
    assert body == {"items": [SAMPLE_ITEM], "total": 1, "page": 1, "page_size": 10}

    _, kwargs = mock_list_categories.call_args
    assert kwargs["user_id"] == fake_user
    assert kwargs["category_type"] == "expense"
    assert kwargs["page"] == 1
    assert kwargs["page_size"] == 10
    assert kwargs["search"] is None
    assert kwargs["sort_by"] == "name"
    assert kwargs["sort_dir"] == "asc"


def test_list_categories_forwards_all_query_params(client, fake_user, fake_pool, monkeypatch):
    mock_list_categories = AsyncMock(return_value=([], 0))
    monkeypatch.setattr("app.api.categories.list_categories", mock_list_categories)

    response = client.get(
        "/categories",
        params={
            "type": "income",
            "page": 2,
            "page_size": 20,
            "search": "sal",
            "sort_by": "name",
            "sort_dir": "desc",
        },
    )

    assert response.status_code == 200
    _, kwargs = mock_list_categories.call_args
    assert kwargs["category_type"] == "income"
    assert kwargs["page"] == 2
    assert kwargs["page_size"] == 20
    assert kwargs["search"] == "sal"
    assert kwargs["sort_dir"] == "desc"


@pytest.mark.parametrize(
    "params",
    [
        {"type": "savings"},
        {"type": "expense", "page_size": 15},
        {"type": "expense", "sort_by": "created_at"},
        {"type": "expense", "sort_dir": "upwards"},
        {"type": "expense", "page": 0},
    ],
)
def test_list_categories_rejects_invalid_query_params(client, fake_user, params):
    response = client.get("/categories", params=params)
    assert response.status_code == 422


def test_list_categories_logs_and_returns_generic_500_on_unexpected_error(
    client, fake_user, fake_pool, monkeypatch, caplog
):
    mock_list_categories = AsyncMock(side_effect=RuntimeError("connection reset, secret=xyz"))
    monkeypatch.setattr("app.api.categories.list_categories", mock_list_categories)

    with caplog.at_level(logging.ERROR):
        response = client.get("/categories", params={"type": "expense"})

    assert response.status_code == 500
    body = response.json()
    assert body["detail"] == "Something went wrong. Please try again."
    assert "secret=xyz" not in body["detail"]

    error_records = [r for r in caplog.records if r.levelno == logging.ERROR]
    assert len(error_records) == 1
    assert error_records[0].exc_info is not None
