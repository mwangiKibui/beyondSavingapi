"""ab-290: admin cleanup of the description keyword list (no approval
gate). Repositories mocked; SQL is checked in the live run."""

from datetime import datetime, timezone
from unittest.mock import AsyncMock
from uuid import uuid4

import asyncpg
import pytest
from fastapi.testclient import TestClient

from app.core.db import get_pool
from app.core.security import get_current_user_id
from app.main import app

API = "app.api.admin_keywords"
NOW = datetime(2026, 11, 18, tzinfo=timezone.utc)
KW_ID = uuid4()


def keyword(**kw):
    return {"id": KW_ID, "pattern": "SALARY", "direction": "in", "category_name": "Salary", "status": "approved",
            "source": "starter", "users_choosing_otherwise": 3, "created_at": NOW, "updated_at": NOW, **kw}


@pytest.fixture(scope="module")
def client():
    with TestClient(app) as c:
        yield c


@pytest.fixture
def pool():
    fake = object()
    app.dependency_overrides[get_pool] = lambda: fake
    yield fake
    app.dependency_overrides.pop(get_pool, None)


def as_role(monkeypatch, role):
    user_id = uuid4()
    app.dependency_overrides[get_current_user_id] = lambda: user_id
    monkeypatch.setattr("app.core.security.get_user_by_id", AsyncMock(return_value={"id": user_id, "role": role}))
    return user_id


@pytest.fixture
def admin(monkeypatch, pool):
    yield as_role(monkeypatch, "admin")
    app.dependency_overrides.pop(get_current_user_id, None)


@pytest.fixture
def repo(monkeypatch):
    mocks = {
        "list_keywords": AsyncMock(return_value=([keyword()], 1)),
        "get_keyword": AsyncMock(return_value=keyword()),
        "get_shared_keyword_choices": AsyncMock(return_value={
            KW_ID: [{"category_name": "Bonus", "users": 1}, {"category_name": "Consulting", "users": 3}]
        }),
        "create_keyword": AsyncMock(return_value=KW_ID),
        "update_keyword": AsyncMock(return_value=True),
    }
    for name, mock in mocks.items():
        monkeypatch.setattr(f"{API}.{name}", mock)
    return mocks


def test_keywords_are_admin_only(client, monkeypatch, pool, repo):
    as_role(monkeypatch, "user")
    try:
        assert client.get("/admin/keywords").status_code == 403
        assert client.post("/admin/keywords", json={"pattern": "BONUS", "direction": "in",
                                                    "category_name": "Salary"}).status_code == 403
    finally:
        app.dependency_overrides.pop(get_current_user_id, None)


def test_list_passes_filters_through(client, admin, repo):
    body = client.get("/admin/keywords?page_size=5&search=sal&direction=in&status=approved").json()
    assert body["total"] == 1 and body["items"][0]["pattern"] == "SALARY"
    kwargs = repo["list_keywords"].await_args.kwargs
    assert (kwargs["search"], kwargs["direction"], kwargs["status"], kwargs["page_size"]) == ("sal", "in",
                                                                                              "approved", 5)


def test_list_rejects_odd_page_sizes(client, admin, repo):
    assert client.get("/admin/keywords?page_size=7").status_code == 422


def test_detail_shows_what_users_chose_instead_most_first(client, admin, repo):
    body = client.get(f"/admin/keywords/{KW_ID}").json()
    assert body["votes"] == [{"category_name": "Consulting", "users": 3}, {"category_name": "Bonus", "users": 1}]


def test_detail_404(client, admin, repo):
    repo["get_keyword"].return_value = None
    assert client.get(f"/admin/keywords/{uuid4()}").status_code == 404


def test_create_cleans_the_pattern(client, admin, repo):
    response = client.post("/admin/keywords", json={"pattern": "  net   pay ", "direction": "in",
                                                    "category_name": " Salary "})
    assert response.status_code == 201
    kwargs = repo["create_keyword"].await_args.kwargs
    assert (kwargs["pattern"], kwargs["category_name"], kwargs["admin_id"]) == ("NET PAY", "Salary", admin)


@pytest.mark.parametrize("pattern", ["", "a", " * ", "X" * 61])
def test_create_rejects_bad_patterns(client, admin, repo, pattern):
    response = client.post("/admin/keywords", json={"pattern": pattern, "direction": "in", "category_name": "Pay"})
    assert response.status_code == 422


def test_create_duplicate_is_409(client, admin, repo):
    repo["create_keyword"].side_effect = asyncpg.UniqueViolationError("dup")
    response = client.post("/admin/keywords", json={"pattern": "SALARY", "direction": "in",
                                                    "category_name": "Salary"})
    assert response.status_code == 409


def test_retire_a_keyword(client, admin, repo):
    assert client.patch(f"/admin/keywords/{KW_ID}", json={"status": "retired"}).status_code == 200
    kwargs = repo["update_keyword"].await_args.kwargs
    assert (kwargs["status"], kwargs["pattern"], kwargs["category_name"]) == ("retired", None, None)


def test_update_404(client, admin, repo):
    repo["update_keyword"].return_value = False
    assert client.patch(f"/admin/keywords/{uuid4()}", json={"category_name": "Pay"}).status_code == 404


def test_errors_are_generic(client, admin, repo):
    repo["list_keywords"].side_effect = RuntimeError("connection string leaked")
    response = client.get("/admin/keywords")
    assert response.status_code == 500 and "leaked" not in response.text
