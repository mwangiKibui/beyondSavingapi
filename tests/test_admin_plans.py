"""ab-309: admin list of plans with their limits, and editing one limit.
Repositories mocked; the SQL is checked in the live run."""

from unittest.mock import AsyncMock
from uuid import uuid4

import pytest
from fastapi.testclient import TestClient

from app.core.db import get_pool
from app.core.security import get_current_user_id
from app.main import app

API = "app.api.admin_plans"
FREE_ID = uuid4()
FREE = {"id": FREE_ID, "key": "free", "name": "Free", "limits": [
    {"limit_key": "accounts_max", "value": None},
    {"limit_key": "ai_layout_proposals_per_day", "value": 5},
    {"limit_key": "statement_imports_per_day", "value": None},
]}


@pytest.fixture(scope="module")
def client():
    with TestClient(app) as c:
        yield c


@pytest.fixture
def pool():
    app.dependency_overrides[get_pool] = lambda: object()
    yield
    app.dependency_overrides.pop(get_pool, None)


def as_role(monkeypatch, role):
    user_id = uuid4()
    app.dependency_overrides[get_current_user_id] = lambda: user_id
    monkeypatch.setattr("app.core.security.get_user_by_id", AsyncMock(return_value={"id": user_id, "role": role}))
    return user_id


@pytest.fixture
def admin(monkeypatch, pool):
    yield as_role(monkeypatch, "super_admin")
    app.dependency_overrides.pop(get_current_user_id, None)


@pytest.fixture
def repo(monkeypatch):
    mocks = {
        "list_plans_with_limits": AsyncMock(return_value=[FREE]),
        "update_plan_limit": AsyncMock(side_effect=lambda conn, *, plan_id, limit_key, value: {
            "limit_key": limit_key, "value": value}),
        "plan_exists": AsyncMock(return_value=True),
    }
    for name, mock in mocks.items():
        monkeypatch.setattr(f"{API}.{name}", mock)
    return mocks


def url(plan_id=FREE_ID, key="statement_imports_per_day"):
    return f"/admin/plans/{plan_id}/limits/{key}"


# --- access -----------------------------------------------------------------


def test_requires_login(client, pool):
    assert client.get("/admin/plans").status_code == 401


def test_non_admin_is_forbidden(client, pool, monkeypatch, repo):
    as_role(monkeypatch, "user")
    try:
        assert client.get("/admin/plans").status_code == 403
        assert client.patch(url(), json={"value": 5}).status_code == 403
    finally:
        app.dependency_overrides.pop(get_current_user_id, None)
    repo["update_plan_limit"].assert_not_called()


# --- list -------------------------------------------------------------------


def test_lists_plans_with_limits(client, admin, repo):
    r = client.get("/admin/plans")
    assert r.status_code == 200
    assert r.json() == [{**FREE, "id": str(FREE_ID)}]


def test_list_error_is_generic(client, admin, repo):
    repo["list_plans_with_limits"].side_effect = RuntimeError("secret detail")
    r = client.get("/admin/plans")
    assert r.status_code == 500 and "secret" not in r.text


# --- patch ------------------------------------------------------------------


@pytest.mark.parametrize("value", [5, 0, None])
def test_sets_a_value_or_unlimited(client, admin, repo, value):
    r = client.patch(url(), json={"value": value})

    assert r.status_code == 200
    assert r.json() == {"limit_key": "statement_imports_per_day", "value": value}
    kwargs = repo["update_plan_limit"].await_args.kwargs
    assert kwargs == {"plan_id": FREE_ID, "limit_key": "statement_imports_per_day", "value": value}


@pytest.mark.parametrize("payload", [{"value": -1}, {}, {"value": "5"}, {"value": 1.5}, {"value": True}])
def test_rejects_bad_values(client, admin, repo, payload):
    assert client.patch(url(), json=payload).status_code == 422
    repo["update_plan_limit"].assert_not_called()


def test_rejects_an_unknown_key(client, admin, repo):
    assert client.patch(url(key="exports_per_day"), json={"value": 1}).status_code == 422
    repo["update_plan_limit"].assert_not_called()


def test_unknown_plan_is_404(client, admin, repo):
    repo["update_plan_limit"].side_effect = None
    repo["update_plan_limit"].return_value = None
    repo["plan_exists"].return_value = False

    r = client.patch(url(plan_id=uuid4()), json={"value": 1})

    assert r.status_code == 404 and r.json()["detail"] == "Plan not found"


def test_plan_without_that_limit_row_is_404(client, admin, repo):
    repo["update_plan_limit"].side_effect = None
    repo["update_plan_limit"].return_value = None

    r = client.patch(url(), json={"value": 1})

    assert r.status_code == 404 and r.json()["detail"] == "This plan has no such limit"
