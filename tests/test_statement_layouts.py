"""ab-315: the mapping wizard API and service."""

from unittest.mock import AsyncMock
from uuid import uuid4

import pytest
from fastapi.testclient import TestClient

from app.core.db import get_pool
from app.core.security import get_current_user_id
from app.main import app
from app.parsers.profiles.schema import validate_profile
from app.services import statement_layouts
from app.services.statement_layouts import check_layout
from tests.statement_pdfs import build, sample_rows
from tests.statement_sheets import HEADER, csv_bytes, rows

IMPORT_ID = uuid4()
PROFILE = {
    "date_format": "%d/%m/%Y",
    "table": {
        "columns": [
            {"role": "date", "header": "Date"},
            {"role": "description", "header": "Details"},
            {"role": "money_in", "header": "Money In"},
            {"role": "money_out", "header": "Money Out"},
            {"role": "balance", "header": "Balance"},
        ],
        "column_bounds": "header_gaps",
        "end_markers": ["End of statement"],
    },
}
NO_BALANCE = {**PROFILE, "table": {**PROFILE["table"], "columns": PROFILE["table"]["columns"][:4]}}
GOOD = build(sample_rows(6))
BAD = build([*sample_rows(3), ["05/10/2026", "Odd", "", "10.00", "5.00"]])


@pytest.fixture(scope="module")
def client():
    with TestClient(app) as c:
        yield c


@pytest.fixture
def api(monkeypatch):
    """A signed-in uploader with one import waiting to be mapped."""
    user_id = uuid4()
    app.dependency_overrides[get_pool] = lambda: object()
    app.dependency_overrides[get_current_user_id] = lambda: user_id
    record = {
        "id": IMPORT_ID,
        "status": "needs_mapping",
        "storage_key": "statements/x/s.pdf",
        "file_name": "s.pdf",
        "fingerprint": '{"v":1}',
        "provider": "Sample Bank",
        "currency": "KES",
        "institution_id": uuid4(),
    }

    class Api:
        pass

    a = Api()
    a.user_id, a.record, a.content = user_id, record, GOOD
    a.get = AsyncMock(side_effect=lambda *x, **k: a.record)
    monkeypatch.setattr("app.api.statement_layouts.get_import_for_layout", a.get)
    monkeypatch.setattr("app.api.statement_layouts.load_content", lambda key: a.content)
    a.status = AsyncMock()
    monkeypatch.setattr("app.api.statement_layouts.set_import_status", a.status)
    a.notify = AsyncMock(return_value=2)
    monkeypatch.setattr("app.api.statement_layouts.notify_admins_layout_waiting", a.notify)
    a.save = AsyncMock(return_value={"id": uuid4(), "version": 1})
    monkeypatch.setattr("app.api.statement_layouts.save_layout", a.save)
    yield a
    app.dependency_overrides.pop(get_pool, None)
    app.dependency_overrides.pop(get_current_user_id, None)


def url(suffix=""):
    return f"/statement-imports/{IMPORT_ID}/layout{suffix}"


# --- check_layout ---------------------------------------------------------------


def test_a_passing_mapping_is_usable_with_ten_preview_rows():
    result = check_layout(build(sample_rows(14)), validate_profile(PROFILE), currency="KES")
    assert result.passed and result.usable and result.row_count == 14
    assert [r["index"] for r in result.rows] == list(range(10))
    assert result.opening_balance == 1000.0


def test_a_failing_mapping_shows_rows_around_the_first_failure():
    result = check_layout(BAD, validate_profile(PROFILE), currency="KES")
    assert not result.passed and not result.usable
    assert result.failing_rows == [3] and result.rows[0]["index"] == 0 and result.message


def test_a_mapping_that_doesnt_fit_says_why():
    wrong = {**PROFILE, "table": {**PROFILE["table"], "columns": [{"role": "date", "header": "Posted"}, *PROFILE["table"]["columns"][1:]]}}
    result = check_layout(GOOD, validate_profile(wrong), currency="KES")
    assert not result.passed and result.row_count == 0 and "headings" in result.message


def test_one_row_isnt_enough():
    result = check_layout(build(sample_rows(1)), validate_profile(PROFILE), currency="KES")
    assert not result.usable and "fewer than 2" in result.message


# --- save_layout ----------------------------------------------------------------


async def test_saving_stores_an_active_shared_layout_and_rereads(monkeypatch):
    created = {"id": uuid4(), "version": 1}
    create = AsyncMock(return_value=created)
    monkeypatch.setattr(statement_layouts, "create_profile", create)
    status = AsyncMock()
    monkeypatch.setattr(statement_layouts, "set_import_status", status)
    queued = []
    monkeypatch.setattr(statement_layouts, "enqueue", AsyncMock(side_effect=lambda i: queued.append(i)))
    waiting = [uuid4(), uuid4()]
    monkeypatch.setattr(statement_layouts, "list_waiting_imports", AsyncMock(return_value=waiting))
    institution, user = uuid4(), uuid4()
    record = {"id": IMPORT_ID, "institution_id": institution, "fingerprint": "fp"}

    out = await statement_layouts.save_layout(object(), statement_import=record, profile=validate_profile(PROFILE), user_id=user)

    assert out == created
    kwargs = create.call_args.kwargs
    assert (kwargs["source"], kwargs["status"], kwargs["created_by"]) == ("user", "active", user)
    assert (kwargs["institution_id"], kwargs["fingerprint"]) == (institution, "fp")
    assert status.call_args.kwargs == {"import_id": IMPORT_ID, "status": "pending"}
    assert queued == [IMPORT_ID, *waiting]


# --- GET layout -----------------------------------------------------------------


def test_the_wizard_starts_with_a_guess_and_page_one(client, api):
    response = client.get(url())
    assert response.status_code == 200
    body = response.json()
    assert body["file_type"] == "pdf" and body["provider"] == "Sample Bank"
    assert [c["role"] for c in body["guess"]["table"]["columns"]] == ["date", "description", "money_in", "money_out", "balance"]
    assert body["page"]["lines"] and body["page_count"] == 1 and body["rows"] is None


def test_a_spreadsheet_starts_with_its_rows(client, api):
    api.record["file_name"], api.content = "export.csv", csv_bytes([HEADER, *rows(3)])
    body = client.get(url()).json()
    assert body["file_type"] == "csv" and body["rows"][0] == HEADER and body["header_row"] == 0 and body["page"] is None


def test_someone_elses_import_is_not_found(client, api):
    api.record = None
    assert client.get(url()).status_code == 404


@pytest.mark.parametrize("state", ["pending", "parsed", "failed"])
def test_an_import_that_doesnt_need_mapping_is_a_conflict(client, api, state):
    api.record["status"] = state
    assert client.get(url()).status_code == 409


def test_an_unexpected_error_is_generic(client, api, monkeypatch):
    monkeypatch.setattr("app.api.statement_layouts.load_content", lambda key: (_ for _ in ()).throw(RuntimeError("s3")))
    response = client.get(url())
    assert response.status_code == 500 and "s3" not in response.text


# --- POST check -----------------------------------------------------------------


def test_checking_a_mapping_returns_rows_and_the_result(client, api):
    response = client.post(url("/check"), json={"profile": PROFILE})
    assert response.status_code == 200
    body = response.json()
    assert body["passed"] and body["usable"] and body["row_count"] == 6 and len(body["rows"]) == 6
    assert body["rows"][0] == {
        "index": 0, "txn_date": "2026-10-01", "description": "Payment from Shop A",
        "amount": 100.0, "direction": "in", "balance_after": 1100.0,
    }
    api.save.assert_not_called()


def test_a_failing_check_names_its_rows(client, api):
    api.content = BAD
    body = client.post(url("/check"), json={"profile": PROFILE}).json()
    assert not body["passed"] and body["failing_rows"] == [3]


def test_an_invalid_mapping_is_422_with_why(client, api):
    response = client.post(url("/check"), json={"profile": {**PROFILE, "date_format": "nonsense"}})
    assert response.status_code == 422 and "date format" in response.json()["detail"]


def test_the_file_type_always_follows_the_upload(client, api):
    api.record["file_name"], api.content = "export.csv", csv_bytes([HEADER, *rows(3)])
    sheet = {**PROFILE, "file_type": "pdf", "table": {**PROFILE["table"], "column_bounds": "cells"}}
    body = client.post(url("/check"), json={"profile": sheet}).json()
    assert body["passed"] and body["row_count"] == 3


# --- PUT layout -----------------------------------------------------------------


def test_saving_a_passing_mapping(client, api):
    response = client.put(url(), json={"profile": PROFILE})
    assert response.status_code == 200 and response.json()["status"] == "pending"
    assert api.save.call_args.kwargs["user_id"] == api.user_id


def test_a_mapping_that_fails_is_never_saved(client, api):
    api.content = BAD
    response = client.put(url(), json={"profile": PROFILE})
    assert response.status_code == 422
    api.save.assert_not_called()


def test_a_mapping_that_proves_nothing_is_never_saved(client, api):
    response = client.put(url(), json={"profile": NO_BALANCE})
    # The profile format itself refuses it: nothing to check against.
    assert response.status_code == 422 and "balance column" in response.json()["detail"]
    api.save.assert_not_called()


def test_a_provider_without_an_institution_cant_save(client, api):
    api.record["institution_id"] = None
    assert client.put(url(), json={"profile": PROFILE}).status_code == 422
    api.save.assert_not_called()


def test_an_upload_waiting_for_support_can_still_be_mapped(client, api):
    api.record["status"] = "awaiting_support"
    assert client.put(url(), json={"profile": PROFILE}).status_code == 200


# --- POST support ---------------------------------------------------------------


def test_asking_for_help_waits_for_support_and_tells_admins(client, api):
    response = client.post(url("/support"))
    assert response.status_code == 200 and response.json() == {"status": "awaiting_support"}
    api.status.assert_awaited_once()
    assert api.status.call_args.kwargs == {"import_id": IMPORT_ID, "status": "awaiting_support"}
    api.notify.assert_awaited_once()
    assert api.notify.call_args.kwargs == {"import_id": IMPORT_ID, "provider": "Sample Bank"}


def test_asking_twice_changes_nothing(client, api):
    api.record["status"] = "awaiting_support"
    assert client.post(url("/support")).status_code == 200
    api.status.assert_not_called()
    api.notify.assert_not_called()


def test_telling_admins_failing_still_waits(client, api):
    api.notify.side_effect = RuntimeError("down")
    assert client.post(url("/support")).status_code == 200
    api.status.assert_awaited_once()
