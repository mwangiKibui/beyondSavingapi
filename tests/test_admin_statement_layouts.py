"""ab-317: admins see, retire, restore and edit statement layouts, and finish
the mapping of uploads waiting for support. Repositories are mocked."""

from contextlib import asynccontextmanager
from datetime import datetime, timezone
from unittest.mock import AsyncMock
from uuid import uuid4

import pytest
from fastapi.testclient import TestClient

from app.core.db import get_pool
from app.core.security import get_current_user_id
from app.main import app
from app.services.admin_statement_layouts import LayoutAdminError, edit_layout, restore_layout, retire_layout
from tests.test_statement_layouts import GOOD, PROFILE

INSTITUTION = uuid4()
ADMIN = uuid4()


def layout(**extra):
    return {"id": uuid4(), "institution_id": INSTITUTION, "fingerprint": '{"v":1}', "profile": PROFILE,
            "version": 2, "source": "user", "status": "active", "usage_count": 4,
            "last_check_passed_at": None, "last_check_failed_at": None, **extra}


class Conn:
    @asynccontextmanager
    async def transaction(self):
        yield


class Pool:
    @asynccontextmanager
    async def acquire(self):
        yield Conn()


@pytest.fixture
def repo(monkeypatch):
    m = {
        "get_profile_for_update": AsyncMock(return_value=None),
        "set_profile_status": AsyncMock(),
        "retire_other_active": AsyncMock(return_value=[]),
        "create_profile": AsyncMock(side_effect=lambda conn, **kw: {**layout(), **kw, "id": uuid4(), "version": 3}),
        "requeue_waiting": AsyncMock(return_value=0),
    }
    for name, mock in m.items():
        monkeypatch.setattr(f"app.services.admin_statement_layouts.{name}", mock)
    return m


# --- the service ---------------------------------------------------------------------


async def test_retire_sets_the_layout_retired(repo):
    row = layout()
    repo["get_profile_for_update"].return_value = row
    await retire_layout(Pool(), profile_id=row["id"])
    assert repo["set_profile_status"].await_args.kwargs == {"profile_id": row["id"], "status": "retired"}
    repo["requeue_waiting"].assert_not_awaited()


async def test_retire_or_restore_of_a_missing_layout_is_not_found(repo):
    for action in (retire_layout, restore_layout):
        with pytest.raises(LayoutAdminError) as exc:
            await action(Pool(), profile_id=uuid4())
        assert exc.value.status_code == 404


async def test_restore_makes_it_the_one_used_and_rereads_waiting_uploads(repo):
    row = layout(status="retired")
    repo["get_profile_for_update"].return_value = row
    await restore_layout(Pool(), profile_id=row["id"])
    assert repo["set_profile_status"].await_args.kwargs == {"profile_id": row["id"], "status": "active"}
    assert repo["retire_other_active"].await_args.kwargs == {
        "institution_id": INSTITUTION, "fingerprint": row["fingerprint"], "keep_id": row["id"]}
    repo["requeue_waiting"].assert_awaited_once()


async def test_an_edit_is_the_next_version_by_an_admin_and_replaces_the_edited_one(repo):
    row = layout()
    repo["get_profile_for_update"].return_value = row
    created = await edit_layout(Pool(), profile_id=row["id"], profile={**PROFILE, "date_format": "%Y-%m-%d"},
                                admin_id=ADMIN)
    kw = repo["create_profile"].await_args.kwargs
    assert (kw["source"], kw["status"], kw["created_by"]) == ("admin", "active", ADMIN)
    assert kw["institution_id"] == INSTITUTION and kw["fingerprint"] == row["fingerprint"]
    assert kw["profile"]["date_format"] == "%Y-%m-%d"
    assert repo["retire_other_active"].await_args.kwargs["keep_id"] == created["id"]
    repo["requeue_waiting"].assert_awaited_once()


async def test_an_invalid_edit_is_refused_before_anything_changes(repo):
    row = layout()
    repo["get_profile_for_update"].return_value = row
    with pytest.raises(LayoutAdminError) as exc:
        await edit_layout(Pool(), profile_id=row["id"], profile={"table": {}}, admin_id=ADMIN)
    assert exc.value.status_code == 422
    repo["create_profile"].assert_not_awaited()
    repo["retire_other_active"].assert_not_awaited()


async def test_a_failed_reread_doesnt_undo_the_change(repo, caplog):
    row = layout(status="retired")
    repo["get_profile_for_update"].return_value = row
    repo["requeue_waiting"].side_effect = RuntimeError("redis down")
    await restore_layout(Pool(), profile_id=row["id"])
    repo["set_profile_status"].assert_awaited_once()
    assert "Re-reading" in caplog.text


# --- the endpoints -------------------------------------------------------------------

API = "app.api.admin_statement_layouts"
WAITING_ID = uuid4()
WAITING = {"id": WAITING_ID, "status": "awaiting_support", "storage_key": "statements/x/s.pdf", "file_name": "s.pdf",
           "fingerprint": '{"v":1}', "provider": "Sample Bank", "currency": "KES", "institution_id": INSTITUTION}


@pytest.fixture(scope="module")
def client():
    with TestClient(app) as c:
        yield c


def as_role(monkeypatch, role):
    app.dependency_overrides[get_pool] = lambda: object()
    app.dependency_overrides[get_current_user_id] = lambda: ADMIN
    monkeypatch.setattr("app.core.security.get_user_by_id", AsyncMock(return_value={"id": ADMIN, "role": role}))


@pytest.fixture
def admin(monkeypatch):
    as_role(monkeypatch, "admin")
    yield
    app.dependency_overrides.pop(get_pool, None)
    app.dependency_overrides.pop(get_current_user_id, None)


@pytest.fixture
def api(monkeypatch):
    now = datetime(2026, 11, 20, tzinfo=timezone.utc)
    m = {
        "list_institution_profiles": AsyncMock(return_value=[layout(last_check_passed_at=now)]),
        "get_profile": AsyncMock(return_value=layout()),
        "retire_layout": AsyncMock(),
        "restore_layout": AsyncMock(),
        "edit_layout": AsyncMock(return_value=layout(version=3, source="admin")),
        "list_awaiting_support": AsyncMock(return_value=([{
            "id": WAITING_ID, "institution_id": INSTITUTION, "institution_name": "Sample Bank",
            "provider": "Sample Bank", "uploaded_at": now, "file_type": "pdf", "fingerprint": '{"v":1}'}], 1)),
        "get_waiting_import": AsyncMock(return_value=dict(WAITING)),
    }
    for name, mock in m.items():
        monkeypatch.setattr(f"{API}.{name}", mock)
    monkeypatch.setattr("app.api.statement_layouts.load_content", lambda key: GOOD)
    m["save_layout"] = AsyncMock(return_value={"id": uuid4(), "version": 1})
    monkeypatch.setattr("app.api.statement_layouts.save_layout", m["save_layout"])
    monkeypatch.setattr("app.api.statement_layouts.latest_proposed_profile", AsyncMock(return_value=None))
    monkeypatch.setattr("app.api.statement_layouts.learned_roles", AsyncMock(return_value={}))
    m["record_choices"] = AsyncMock()
    monkeypatch.setattr("app.api.statement_layouts.record_choices", m["record_choices"])
    return m


PATHS = [
    ("get", f"/admin/institutions/{INSTITUTION}/statement-layouts", None),
    ("get", f"/admin/statement-layouts/{uuid4()}", None),
    ("post", f"/admin/statement-layouts/{uuid4()}/retire", None),
    ("post", f"/admin/statement-layouts/{uuid4()}/restore", None),
    ("put", f"/admin/statement-layouts/{uuid4()}", {"profile": PROFILE}),
    ("get", "/admin/statement-imports/awaiting-support", None),
    ("get", f"/admin/statement-imports/{WAITING_ID}/layout", None),
    ("post", f"/admin/statement-imports/{WAITING_ID}/layout/check", {"profile": PROFILE}),
    ("put", f"/admin/statement-imports/{WAITING_ID}/layout", {"profile": PROFILE}),
]


@pytest.mark.parametrize("method,path,body", PATHS)
def test_everything_needs_sign_in(client, method, path, body):
    assert client.request(method, path, json=body).status_code == 401


@pytest.mark.parametrize("method,path,body", PATHS)
def test_everything_is_admin_only(client, monkeypatch, api, method, path, body):
    as_role(monkeypatch, "user")
    try:
        assert client.request(method, path, json=body).status_code == 403
    finally:
        app.dependency_overrides.pop(get_pool, None)
        app.dependency_overrides.pop(get_current_user_id, None)


def test_list_an_institutions_layouts(client, admin, api):
    response = client.get(f"/admin/institutions/{INSTITUTION}/statement-layouts")
    assert response.status_code == 200
    (item,) = response.json()["items"]
    assert (item["source"], item["status"], item["version"], item["usage_count"]) == ("user", "active", 2, 4)
    assert item["last_check"] == {"result": "passed", "at": "2026-11-20T00:00:00Z"}
    assert "profile" not in item
    assert api["list_institution_profiles"].await_args.kwargs == {"institution_id": INSTITUTION}


def test_last_check_is_the_latest_result():
    from app.api.admin_statement_layouts import last_check
    early, late = datetime(2026, 11, 1, tzinfo=timezone.utc), datetime(2026, 11, 2, tzinfo=timezone.utc)
    assert last_check({"last_check_passed_at": early, "last_check_failed_at": late})["result"] == "failed"
    assert last_check({"last_check_passed_at": late, "last_check_failed_at": early})["result"] == "passed"
    assert last_check({"last_check_passed_at": None, "last_check_failed_at": None}) is None


def test_get_one_layout_with_its_settings(client, admin, api):
    response = client.get(f"/admin/statement-layouts/{uuid4()}")
    assert response.status_code == 200 and response.json()["profile"] == PROFILE


def test_get_a_missing_layout_is_404(client, admin, api):
    api["get_profile"].return_value = None
    assert client.get(f"/admin/statement-layouts/{uuid4()}").status_code == 404


def test_retire_restore_and_edit_pass_through(client, admin, api):
    pid = uuid4()
    assert client.post(f"/admin/statement-layouts/{pid}/retire").status_code == 200
    assert api["retire_layout"].await_args.kwargs["profile_id"] == pid
    assert client.post(f"/admin/statement-layouts/{pid}/restore").status_code == 200
    assert api["restore_layout"].await_args.kwargs["profile_id"] == pid
    response = client.put(f"/admin/statement-layouts/{pid}", json={"profile": PROFILE})
    assert response.status_code == 200 and response.json()["source"] == "admin"
    kw = api["edit_layout"].await_args.kwargs
    assert kw["profile_id"] == pid and kw["admin_id"] == ADMIN and kw["profile"] == PROFILE


def test_a_service_refusal_keeps_its_status(client, admin, api):
    api["edit_layout"].side_effect = LayoutAdminError(422, "date_format is invalid")
    response = client.put(f"/admin/statement-layouts/{uuid4()}", json={"profile": PROFILE})
    assert response.status_code == 422 and response.json()["detail"] == "date_format is invalid"


def test_an_unexpected_error_is_generic_and_logged(client, admin, api, caplog):
    api["retire_layout"].side_effect = RuntimeError("boom secret")
    response = client.post(f"/admin/statement-layouts/{uuid4()}/retire")
    assert response.status_code == 500 and "boom" not in response.text
    assert "statement layout" in caplog.text


def test_the_queue_shows_no_uploader_or_file_name(client, admin, api):
    response = client.get("/admin/statement-imports/awaiting-support?page=1&page_size=10")
    assert response.status_code == 200
    body = response.json()
    assert body["total"] == 1
    (item,) = body["items"]
    assert item["institution_name"] == "Sample Bank" and item["file_type"] == "pdf"
    assert not {"file_name", "user_id", "email", "account_id"} & set(item)
    assert api["list_awaiting_support"].await_args.kwargs == {"limit": 10, "offset": 0}


def test_an_admin_starts_and_checks_a_waiting_upload(client, admin, api):
    start = client.get(f"/admin/statement-imports/{WAITING_ID}/layout")
    assert start.status_code == 200 and start.json()["file_type"] == "pdf"
    check = client.post(f"/admin/statement-imports/{WAITING_ID}/layout/check", json={"profile": PROFILE})
    assert check.status_code == 200 and check.json()["usable"] is True
    assert api["get_waiting_import"].await_args.kwargs == {"import_id": WAITING_ID}


def test_an_admin_saves_a_layout_as_admin(client, admin, api):
    response = client.put(f"/admin/statement-imports/{WAITING_ID}/layout", json={"profile": PROFILE})
    assert response.status_code == 200 and response.json()["status"] == "pending"
    kw = api["save_layout"].await_args.kwargs
    assert kw["source"] == "admin" and kw["user_id"] == ADMIN
    # An admin's choices don't teach the users' heading suggestions.
    api["record_choices"].assert_not_awaited()


def test_only_uploads_waiting_for_support_can_be_opened(client, admin, api):
    api["get_waiting_import"].return_value = None
    assert client.get(f"/admin/statement-imports/{WAITING_ID}/layout").status_code == 404
