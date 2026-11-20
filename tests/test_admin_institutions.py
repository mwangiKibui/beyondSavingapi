"""ab-197/198/199: admin create / update / retire institutions (ab-184's
table). Repositories mocked; the SQL is checked in the live run."""

from contextlib import asynccontextmanager
from datetime import datetime, timezone
from unittest.mock import AsyncMock
from uuid import uuid4

import asyncpg
import pytest
from fastapi.testclient import TestClient

from app.core.db import get_pool
from app.core.security import get_current_user_id
from app.main import app
from app.parsers import STATEMENT_PROVIDERS
from app.repositories.institutions import normalize_name

API = "app.api.admin_institutions"
NOW = datetime(2026, 11, 20, tzinfo=timezone.utc)


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


def institution(**kw):
    return {"id": uuid4(), "name": "Sample Bank", "type": "bank", "regulator": "CBK",
            "statements_uploadable": False, "retired_at": None, "created_at": NOW, "updated_at": NOW, **kw}


@pytest.fixture
def repo(monkeypatch):
    mocks = {
        "list_institutions": AsyncMock(return_value=([institution()], 1)),
        "get_institution": AsyncMock(return_value=institution()),
        "find_duplicate": AsyncMock(return_value=None),
        "create_institution": AsyncMock(side_effect=lambda conn, **kw: institution(**kw)),
        "update_institution": AsyncMock(side_effect=lambda conn, *, institution_id, changes: institution(
            id=institution_id, **changes)),
        "count_accounts_using": AsyncMock(return_value=0),
        "rename_account_providers": AsyncMock(return_value=0),
        "set_retired": AsyncMock(side_effect=lambda conn, *, institution_id, retired: institution(
            id=institution_id, retired_at=NOW if retired else None)),
    }
    for name, mock in mocks.items():
        monkeypatch.setattr(f"{API}.{name}", mock)
    return mocks


NEW = {"name": "Sample Bank", "type": "bank", "regulator": "CBK"}


# --- access ---------------------------------------------------------------


def test_requires_login(client, pool):
    assert client.get("/admin/institutions").status_code == 401


def test_non_admin_is_forbidden_everywhere(client, pool, monkeypatch, repo):
    user_id = uuid4()
    app.dependency_overrides[get_current_user_id] = lambda: user_id
    monkeypatch.setattr("app.core.security.get_user_by_id", AsyncMock(return_value={"id": user_id, "role": "user"}))
    try:
        some = uuid4()
        calls = [("get", "/admin/institutions", None), ("get", f"/admin/institutions/{some}", None),
                 ("post", "/admin/institutions", NEW), ("patch", f"/admin/institutions/{some}", {"name": "X"}),
                 ("post", f"/admin/institutions/{some}/retire", None),
                 ("post", f"/admin/institutions/{some}/restore", None)]
        for method, url, body in calls:
            kwargs = {"json": body} if body is not None else {}
            assert getattr(client, method)(url, **kwargs).status_code == 403, url
    finally:
        app.dependency_overrides.pop(get_current_user_id, None)
    repo["create_institution"].assert_not_called()
    repo["update_institution"].assert_not_called()
    repo["set_retired"].assert_not_called()


# --- ab-197 create ----------------------------------------------------------


def test_create_cleans_name_and_defaults(client, admin, repo, pool):
    r = client.post("/admin/institutions", json={"name": "  Sample   Bank ", "type": "bank"})
    assert r.status_code == 201
    body = r.json()
    assert body["name"] == "Sample Bank"
    assert body["statements_uploadable"] is False
    assert body["is_active"] is True
    kwargs = repo["create_institution"].call_args.kwargs
    assert kwargs["name"] == "Sample Bank" and kwargs["regulator"] is None
    assert pool.log == ["begin", "commit"]


@pytest.mark.parametrize("payload", [
    {"name": "   ", "type": "bank"},
    {"name": "x" * 101, "type": "bank"},
    {"name": "Sample", "type": "casino"},
    {"name": "Sample", "type": "bank", "regulator": "FBI"},
])
def test_create_rejects_bad_input(client, admin, repo, payload):
    assert client.post("/admin/institutions", json=payload).status_code == 422
    repo["create_institution"].assert_not_called()


def test_create_duplicate_is_409(client, admin, repo):
    repo["find_duplicate"].return_value = institution(name="Sample Bank")
    r = client.post("/admin/institutions", json={"name": "sample  bank", "type": "bank"})
    assert r.status_code == 409
    assert "Sample Bank" in r.json()["detail"]
    repo["create_institution"].assert_not_called()


def test_create_race_is_409(client, admin, repo):
    repo["create_institution"].side_effect = asyncpg.UniqueViolationError("dup")
    assert client.post("/admin/institutions", json=NEW).status_code == 409


def test_uploadable_needs_a_statement_reader(client, admin, repo):
    r = client.post("/admin/institutions", json={"name": "Sample Sacco", "type": "sacco",
                                                  "statements_uploadable": True})
    assert r.status_code == 422
    assert "statement" in r.json()["detail"].lower()
    ok = client.post("/admin/institutions", json={"name": "Equity Bank", "type": "bank",
                                                   "statements_uploadable": True})
    assert ok.status_code == 201


def test_create_server_error_is_generic(client, admin, repo):
    repo["create_institution"].side_effect = RuntimeError("boom secret")
    r = client.post("/admin/institutions", json=NEW)
    assert r.status_code == 500
    assert "boom" not in r.text


# --- list / get ---------------------------------------------------------------


def test_list_passes_filters_and_paging(client, admin, repo):
    r = client.get("/admin/institutions?search=bank&type=bank&status=retired&page=2&page_size=20")
    assert r.status_code == 200
    assert r.json()["total"] == 1 and r.json()["page"] == 2
    kwargs = repo["list_institutions"].call_args.kwargs
    assert kwargs == {"search": "bank", "type": "bank", "status": "retired", "page": 2, "page_size": 20}


def test_list_defaults_and_bad_page_size(client, admin, repo):
    client.get("/admin/institutions")
    assert repo["list_institutions"].call_args.kwargs["status"] == "all"
    assert client.get("/admin/institutions?page_size=7").status_code == 422


def test_get_missing_is_404(client, admin, repo):
    repo["get_institution"].return_value = None
    assert client.get(f"/admin/institutions/{uuid4()}").status_code == 404


# --- ab-198 update ----------------------------------------------------------


def test_update_missing_is_404(client, admin, repo):
    repo["get_institution"].return_value = None
    assert client.patch(f"/admin/institutions/{uuid4()}", json={"name": "X"}).status_code == 404


def test_update_empty_body_is_422(client, admin, repo):
    assert client.patch(f"/admin/institutions/{uuid4()}", json={}).status_code == 422


def test_rename_onto_existing_is_409(client, admin, repo):
    repo["find_duplicate"].return_value = institution(name="Other Bank")
    r = client.patch(f"/admin/institutions/{uuid4()}", json={"name": "other bank"})
    assert r.status_code == 409
    repo["update_institution"].assert_not_called()


def test_rename_updates_account_providers_in_one_transaction(client, admin, repo, pool):
    iid = uuid4()
    repo["get_institution"].return_value = institution(id=iid, name="Old Bank")
    r = client.patch(f"/admin/institutions/{iid}", json={"name": "New Bank"})
    assert r.status_code == 200
    repo["rename_account_providers"].assert_awaited_once()
    assert repo["rename_account_providers"].call_args.kwargs == {"old": "Old Bank", "new": "New Bank", "type": "bank"}
    assert pool.log == ["begin", "commit"]


def test_rename_rolls_back_when_a_later_step_fails(client, admin, repo, pool):
    repo["get_institution"].return_value = institution(name="Old Bank")
    repo["update_institution"].side_effect = RuntimeError("boom")
    assert client.patch(f"/admin/institutions/{uuid4()}", json={"name": "New Bank"}).status_code == 500
    assert pool.log == ["begin", "rollback"]


def test_rename_of_parser_backed_provider_is_422(client, admin, repo):
    repo["get_institution"].return_value = institution(name="M-Pesa", type="mobile_money")
    r = client.patch(f"/admin/institutions/{uuid4()}", json={"name": "MPESA"})
    assert r.status_code == 422
    repo["update_institution"].assert_not_called()


def test_type_change_blocked_while_accounts_use_it(client, admin, repo):
    repo["count_accounts_using"].return_value = 3
    assert client.patch(f"/admin/institutions/{uuid4()}", json={"type": "sacco"}).status_code == 409
    repo["count_accounts_using"].return_value = 0
    assert client.patch(f"/admin/institutions/{uuid4()}", json={"type": "sacco"}).status_code == 200


def test_regulator_null_clears_but_missing_leaves_it(client, admin, repo):
    client.patch(f"/admin/institutions/{uuid4()}", json={"regulator": None})
    assert repo["update_institution"].call_args.kwargs["changes"] == {"regulator": None}
    client.patch(f"/admin/institutions/{uuid4()}", json={"name": "Renamed Bank"})
    assert "regulator" not in repo["update_institution"].call_args.kwargs["changes"]


def test_update_cannot_mark_uploadable_without_reader(client, admin, repo):
    repo["get_institution"].return_value = institution(name="Sample Sacco", type="sacco")
    r = client.patch(f"/admin/institutions/{uuid4()}", json={"statements_uploadable": True})
    assert r.status_code == 422


# --- ab-199 retire / restore ------------------------------------------------


def test_retire_and_restore(client, admin, repo):
    iid = uuid4()
    r = client.post(f"/admin/institutions/{iid}/retire")
    assert r.status_code == 200 and r.json()["is_active"] is False
    assert repo["set_retired"].call_args.kwargs == {"institution_id": iid, "retired": True}
    r = client.post(f"/admin/institutions/{iid}/restore")
    assert r.status_code == 200 and r.json()["is_active"] is True


def test_retire_twice_is_fine(client, admin, repo):
    repo["get_institution"].return_value = institution(retired_at=NOW)
    r = client.post(f"/admin/institutions/{uuid4()}/retire")
    assert r.status_code == 200 and r.json()["is_active"] is False
    repo["set_retired"].assert_not_called()


def test_retire_missing_is_404(client, admin, repo):
    repo["get_institution"].return_value = None
    assert client.post(f"/admin/institutions/{uuid4()}/retire").status_code == 404


def test_there_is_no_delete(client, admin, repo):
    assert client.delete(f"/admin/institutions/{uuid4()}").status_code == 405


# --- helpers ----------------------------------------------------------------


def test_statement_providers():
    assert STATEMENT_PROVIDERS == {"M-Pesa", "Equity Bank", "NCBA Bank", "Mentor Sacco"}


@pytest.mark.parametrize("raw,expected", [
    ("  Etica  Money Market Fund", "eticamoneymarketfund"),
    ("EQUITY\tBank", "equitybank"),
    ("M-Pesa", "m-pesa"),
])
def test_normalize_name_matches_the_db_index(raw, expected):
    assert normalize_name(raw) == expected
