"""ab-253 PR 3: admin cleanup of the shared merchant list (no approval
queue). Repositories mocked; SQL is checked in the live run."""

from contextlib import asynccontextmanager
from datetime import datetime, timezone
from unittest.mock import AsyncMock
from uuid import uuid4

import pytest
from fastapi.testclient import TestClient

from app.core.db import get_pool
from app.core.security import get_current_user_id
from app.main import app

API = "app.api.admin_merchants"
NOW = datetime(2026, 11, 10, tzinfo=timezone.utc)


class FakeConn:
    def __init__(self, log):
        self.log = log

    @asynccontextmanager
    async def transaction(self):
        self.log.append("begin")
        try:
            yield
        except Exception:
            self.log.append("rollback")
            raise
        self.log.append("commit")


class FakePool:
    def __init__(self):
        self.log = []

    @asynccontextmanager
    async def acquire(self):
        yield FakeConn(self.log)


@pytest.fixture(scope="module")
def client():
    with TestClient(app) as c:
        yield c


@pytest.fixture
def pool():
    fake = FakePool()
    app.dependency_overrides[get_pool] = lambda: fake
    yield fake
    app.dependency_overrides.pop(get_pool, None)


@pytest.fixture
def admin(monkeypatch, pool):
    user_id = uuid4()
    app.dependency_overrides[get_current_user_id] = lambda: user_id
    monkeypatch.setattr("app.core.security.get_user_by_id", AsyncMock(return_value={"id": user_id, "role": "admin"}))
    yield user_id
    app.dependency_overrides.pop(get_current_user_id, None)


def merchant(**kw):
    return {"id": uuid4(), "display_name": "Naivas", "category_name": "Groceries", "direction": "out",
            "source": "starter", "status": "approved", "notes": None,
            "keys": [{"key_kind": "name_pattern", "key_value": "NAIVAS", "requires_text": False}],
            "votes": [{"category_name": "Groceries", "users": 4}], "created_at": NOW, "updated_at": NOW, **kw}


@pytest.fixture
def repo(monkeypatch):
    mocks = {
        "list_merchants": AsyncMock(return_value=([merchant()], 1)),
        "get_merchant": AsyncMock(return_value=merchant()),
        "get_blocked": AsyncMock(return_value=[]),
        "find_conflicts": AsyncMock(return_value=[]),
        "create_merchant": AsyncMock(return_value=uuid4()),
        "update_merchant": AsyncMock(),
        "delete_merchant": AsyncMock(return_value=True),
        "list_from_users": AsyncMock(return_value=([], 0)),
        "list_unknown_numbers": AsyncMock(return_value=([], 0)),
        "dismiss_unknown_number": AsyncMock(),
    }
    for name, mock in mocks.items():
        monkeypatch.setattr(f"{API}.{name}", mock)
    return mocks


NEW = {"display_name": "  Mama Pima  Hardware ", "category_name": "Household", "direction": "out",
       "keys": [{"key_kind": "name_pattern", "key_value": "mama  pima"}, {"key_kind": "till", "key_value": "6999999"}]}


# --- access ----------------------------------------------------------------


def test_requires_auth(client):
    assert client.get("/admin/merchants").status_code == 401


def test_non_admin_is_forbidden(client, pool, monkeypatch, repo):
    user_id = uuid4()
    app.dependency_overrides[get_current_user_id] = lambda: user_id
    monkeypatch.setattr("app.core.security.get_user_by_id", AsyncMock(return_value={"id": user_id, "role": "user"}))
    try:
        for method, url in [("get", "/admin/merchants"), ("post", "/admin/merchants"),
                            ("get", "/admin/unknown-numbers"), ("get", "/admin/merchants/from-users")]:
            assert getattr(client, method)(url, **({"json": NEW} if method == "post" else {})).status_code == 403
    finally:
        app.dependency_overrides.pop(get_current_user_id, None)
    repo["create_merchant"].assert_not_awaited()


# --- list / detail -----------------------------------------------------------


def test_list_passes_filters_and_returns_votes(client, admin, repo):
    response = client.get("/admin/merchants?page=2&page_size=20&search=naiv&source=starter&direction=out")
    assert response.status_code == 200
    body = response.json()
    assert body["total"] == 1 and body["page"] == 2 and body["page_size"] == 20
    assert body["items"][0]["votes"] == [{"category_name": "Groceries", "users": 4}]
    kwargs = repo["list_merchants"].call_args.kwargs
    assert (kwargs["search"], kwargs["source"], kwargs["direction"], kwargs["page"]) == ("naiv", "starter", "out", 2)


def test_list_rejects_other_page_sizes(client, admin, repo):
    assert client.get("/admin/merchants?page_size=7").status_code == 422


def test_vote_data_never_names_users(client, admin, repo):
    assert "user_id" not in client.get("/admin/merchants").text


def test_get_one_404(client, admin, repo):
    repo["get_merchant"].return_value = None
    assert client.get(f"/admin/merchants/{uuid4()}").status_code == 404


def test_list_error_is_generic_500(client, admin, repo):
    repo["list_merchants"].side_effect = RuntimeError("db exploded")
    response = client.get("/admin/merchants")
    assert response.status_code == 500 and "exploded" not in response.text


# --- create ----------------------------------------------------------------


def test_create_cleans_input_and_commits(client, admin, pool, repo):
    response = client.post("/admin/merchants", json=NEW)
    assert response.status_code == 201
    kwargs = repo["create_merchant"].call_args.kwargs
    assert kwargs["display_name"] == "Mama Pima Hardware"
    assert kwargs["admin_id"] == admin
    assert kwargs["keys"] == [{"key_kind": "name_pattern", "key_value": "MAMA PIMA", "requires_text": False},
                              {"key_kind": "till", "key_value": "6999999", "requires_text": False}]
    assert pool.log == ["begin", "commit"]


@pytest.mark.parametrize("keys", [
    [],
    [{"key_kind": "till", "key_value": "12"}],
    [{"key_kind": "paybill", "key_value": "12ab34"}],
    [{"key_kind": "name_pattern", "key_value": " * "}],
    [{"key_kind": "name_pattern", "key_value": "NAIVAS", "requires_text": True}],
    [{"key_kind": "reference", "key_value": "123456"}],
])
def test_create_rejects_bad_keys(client, admin, repo, keys):
    assert client.post("/admin/merchants", json={**NEW, "keys": keys}).status_code == 422
    repo["create_merchant"].assert_not_awaited()


def test_create_rejects_blank_names(client, admin, repo):
    assert client.post("/admin/merchants", json={**NEW, "display_name": "   "}).status_code == 422
    assert client.post("/admin/merchants", json={**NEW, "category_name": ""}).status_code == 422


def test_create_409_on_clash_and_rolls_back(client, admin, pool, repo):
    repo["find_conflicts"].return_value = ['Till 6999999 already belongs to "Rubis"']
    response = client.post("/admin/merchants", json=NEW)
    assert response.status_code == 409
    assert response.json()["detail"] == 'Till 6999999 already belongs to "Rubis"'
    repo["create_merchant"].assert_not_awaited()
    assert pool.log == ["begin", "rollback"]


def test_create_rejects_a_blocked_number(client, admin, repo):
    repo["get_blocked"].return_value = [{"number_kind": "paybill", "number": "222222", "belongs_to": "eCitizen"}]
    response = client.post("/admin/merchants", json={**NEW, "keys": [{"key_kind": "paybill", "key_value": "222222"}]})
    assert response.status_code == 422
    assert "222222" in response.json()["detail"]


def test_duplicate_keys_in_one_request_are_merged(client, admin, repo):
    keys = [{"key_kind": "till", "key_value": "6999999"}, {"key_kind": "till", "key_value": " 6999999 "}]
    client.post("/admin/merchants", json={**NEW, "keys": keys})
    assert len(repo["create_merchant"].call_args.kwargs["keys"]) == 1


# --- update / delete -------------------------------------------------------


def test_update_keeps_unchanged_fields(client, admin, repo):
    mid = uuid4()
    response = client.patch(f"/admin/merchants/{mid}", json={"category_name": "Household"})
    assert response.status_code == 200
    kwargs = repo["update_merchant"].call_args.kwargs
    assert (kwargs["display_name"], kwargs["category_name"], kwargs["keys"]) == ("Naivas", "Household", None)
    assert repo["find_conflicts"].call_args.kwargs["exclude_merchant_id"] == mid


def test_update_404(client, admin, repo):
    repo["get_merchant"].return_value = None
    assert client.patch(f"/admin/merchants/{uuid4()}", json={"category_name": "X"}).status_code == 404
    repo["update_merchant"].assert_not_awaited()


def test_update_rejects_an_empty_key_set(client, admin, repo):
    assert client.patch(f"/admin/merchants/{uuid4()}", json={"keys": []}).status_code == 422


def test_delete(client, admin, repo):
    assert client.delete(f"/admin/merchants/{uuid4()}").status_code == 204
    repo["delete_merchant"].return_value = False
    assert client.delete(f"/admin/merchants/{uuid4()}").status_code == 404


# --- from users / unknown numbers -----------------------------------------


def test_from_users_uses_the_shared_threshold(client, admin, repo):
    repo["list_from_users"].return_value = ([{"key_kind": "name", "key_value": "MAMA PIMA HARDWARE",
                                              "direction": "out", "users": 3, "on_list": False,
                                              "votes": [{"category_name": "Household", "users": 3}]}], 1)
    body = client.get("/admin/merchants/from-users").json()
    assert body["items"][0]["key_value"] == "MAMA PIMA HARDWARE"
    assert repo["list_from_users"].call_args.kwargs["min_users"] == 3


def test_unknown_numbers_need_ten_users(client, admin, repo):
    assert client.get("/admin/unknown-numbers").status_code == 200
    assert repo["list_unknown_numbers"].call_args.kwargs["min_users"] == 10


def test_dismiss_unknown_number(client, admin, repo):
    body = {"number_kind": "till", "number": "9120385", "direction": "out"}
    assert client.post("/admin/unknown-numbers/dismiss", json=body).status_code == 204
    assert repo["dismiss_unknown_number"].call_args.kwargs["number"] == "9120385"
    assert client.post("/admin/unknown-numbers/dismiss", json={**body, "number": "12"}).status_code == 422
