"""ab-265: save / load / discard an unfinished import review so the upload
wizard can resume it on any device, and the list can show "N of M"."""

from datetime import date, datetime, timezone
from unittest.mock import AsyncMock
from uuid import uuid4

import pytest
from fastapi.testclient import TestClient

from app.core.db import get_pool
from app.core.security import get_current_user_id
from app.main import app
from app.services.import_progress import progress_counts
from app.services.category_suggestions import extract_keys

API = "app.api.statement_imports"
SECRET = "dev-suggestion-secret-change-me"
IMPORT_ID = uuid4()
GROCERIES = {"id": uuid4(), "name": "Groceries", "type": "expense"}
UPDATED = datetime(2026, 11, 11, 12, 0, tzinfo=timezone.utc)


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


def import_row(status="parsed", reviewed_at=None):
    return {"id": IMPORT_ID, "account_id": uuid4(), "account_nickname": "My M-Pesa", "provider": "M-Pesa",
            "file_name": "statement.pdf", "status": status, "period_start": date(2026, 10, 1),
            "period_end": date(2026, 10, 31), "row_count": 3, "reviewed_at": reviewed_at,
            "created_at": datetime(2026, 11, 1, tzinfo=timezone.utc)}


def txn(description, *, direction="out", allocated=0, is_transfer=False):
    return {"id": uuid4(), "txn_date": date(2026, 10, 5), "amount": 1000.0, "currency": "KES",
            "direction": direction, "description": description, "is_transfer": is_transfer,
            "allocated": allocated, "category_id": None, "balance_after": None}


NAIVAS = txn("Merchant Payment to 5555555 - NAIVAS LTD")
SHELL = txn("Merchant Payment to 5555556 - SHELL KAREN")
PAYOUT = txn("M-Shwari Loan Disburse", direction="in")
DONE = txn("Merchant Payment to 5555557 - JAVA HOUSE", allocated=1000.0)
ROWS = [NAIVAS, SHELL, PAYOUT, DONE]


def patch_repo(monkeypatch, *, imp=None, progress=None, rows=ROWS, categories=(GROCERIES,)):
    saved = {"step": "review", "draft": {}, "reviewed_count": 0, "total_count": 0, "updated_at": UPDATED}

    async def upsert(pool, *, import_id, step, draft, reviewed_count, total_count):
        return {**saved, "step": step, "draft": draft, "reviewed_count": reviewed_count,
                "total_count": total_count}

    mocks = {
        "get_import_for_preview": AsyncMock(return_value=imp),
        "get_progress": AsyncMock(return_value=progress),
        "get_import_rows": AsyncMock(return_value=list(rows)),
        "get_user_categories": AsyncMock(return_value=list(categories)),
        "upsert_progress": AsyncMock(side_effect=upsert),
        "delete_progress": AsyncMock(return_value=True),
    }
    for name, mock in mocks.items():
        monkeypatch.setattr(f"{API}.{name}", mock)
    return mocks


def url(suffix=""):
    return f"/statement-imports/{IMPORT_ID}/progress{suffix}"


# --- auth / ownership ----------------------------------------------------------


def test_progress_requires_auth(client):
    assert client.get(url()).status_code == 401
    assert client.put(url(), json={"step": "review", "draft": {}}).status_code == 401
    assert client.delete(url()).status_code == 401


def test_progress_returns_503_when_db_unreachable(client, fake_user):
    assert client.get(url()).status_code == 503


def test_another_users_import_is_not_found(client, fake_user, fake_pool, monkeypatch):
    patch_repo(monkeypatch, imp=None)
    assert client.get(url()).status_code == 404
    assert client.put(url(), json={"step": "review", "draft": {}}).status_code == 404
    assert client.delete(url()).status_code == 404


# --- GET ---------------------------------------------------------------------------


@pytest.mark.parametrize("status,reviewed_at,step", [
    ("pending", None, "processing"),
    ("failed", None, "failed"),
    ("parsed", None, "review"),
    ("parsed", UPDATED, "done"),
])
def test_without_saved_progress_the_step_comes_from_the_import(client, fake_user, fake_pool, monkeypatch,
                                                               status, reviewed_at, step):
    patch_repo(monkeypatch, imp=import_row(status, reviewed_at))
    body = client.get(url()).json()
    assert body == {"step": step, "draft": {"rows": {}, "labels": {}, "paybills": {}},
                    "reviewed_count": 0, "total_count": 0, "updated_at": None}


def test_saved_progress_is_returned(client, fake_user, fake_pool, monkeypatch):
    draft = {"rows": {str(NAIVAS["id"]): {"category_id": str(GROCERIES["id"])}}, "labels": {}, "paybills": {}}
    patch_repo(monkeypatch, imp=import_row(), progress={"step": "review", "draft": draft, "reviewed_count": 1,
                                                        "total_count": 2, "updated_at": UPDATED})
    body = client.get(url()).json()
    assert body["step"] == "review" and body["reviewed_count"] == 1 and body["total_count"] == 2
    assert body["draft"]["rows"] == {str(NAIVAS["id"]): {"category_id": str(GROCERIES["id"]),
                                                         "new_category_name": None, "unlabelled": False}}


# --- PUT -----------------------------------------------------------------------------


def test_save_stores_the_draft_and_counts(client, fake_user, fake_pool, monkeypatch):
    mocks = patch_repo(monkeypatch, imp=import_row())
    draft = {
        "rows": {str(NAIVAS["id"]): {"category_id": str(GROCERIES["id"])},
                 str(SHELL["id"]): {"new_category_name": "  Fuel  "}},
        "labels": {}, "paybills": {},
    }
    response = client.put(url(), json={"step": "review", "draft": draft})
    assert response.status_code == 200, response.text
    kwargs = mocks["upsert_progress"].call_args.kwargs
    assert kwargs["import_id"] == IMPORT_ID and kwargs["step"] == "review"
    assert kwargs["draft"]["rows"][str(SHELL["id"])]["new_category_name"] == "Fuel"
    # Open, non-labelled rows: NAIVAS and SHELL. PAYOUT is a loan label, DONE is already allocated.
    assert (kwargs["reviewed_count"], kwargs["total_count"]) == (2, 2)
    assert response.json()["reviewed_count"] == 2


def test_save_while_processing_is_a_conflict(client, fake_user, fake_pool, monkeypatch):
    patch_repo(monkeypatch, imp=import_row("pending"))
    assert client.put(url(), json={"step": "review", "draft": {}}).status_code == 409


def test_only_the_review_step_can_be_saved(client, fake_user, fake_pool, monkeypatch):
    patch_repo(monkeypatch, imp=import_row())
    assert client.put(url(), json={"step": "processing", "draft": {}}).status_code == 422


@pytest.mark.parametrize("row_entry", [
    {"category_id": str(uuid4()), "new_category_name": "Pets"},  # both
    {},  # neither and not unlabelled
    {"new_category_name": "x" * 61},
])
def test_invalid_row_entries_are_rejected(client, fake_user, fake_pool, monkeypatch, row_entry):
    patch_repo(monkeypatch, imp=import_row())
    draft = {"rows": {str(NAIVAS["id"]): row_entry}}
    assert client.put(url(), json={"step": "review", "draft": draft}).status_code == 422


def test_a_row_from_outside_this_import_is_rejected(client, fake_user, fake_pool, monkeypatch):
    patch_repo(monkeypatch, imp=import_row())
    draft = {"rows": {str(uuid4()): {"category_id": str(GROCERIES["id"])}}}
    response = client.put(url(), json={"step": "review", "draft": draft})
    assert response.status_code == 422
    assert "isn't part of this statement" in response.json()["detail"]


def test_another_users_category_is_rejected(client, fake_user, fake_pool, monkeypatch):
    patch_repo(monkeypatch, imp=import_row())
    draft = {"rows": {str(NAIVAS["id"]): {"category_id": str(uuid4())}}}
    response = client.put(url(), json={"step": "review", "draft": draft})
    assert response.status_code == 422
    assert "category" in response.json()["detail"].lower()


def test_labels_and_paybills_must_be_for_this_imports_payees(client, fake_user, fake_pool, monkeypatch):
    patch_repo(monkeypatch, imp=import_row())
    assert client.put(url(), json={"step": "review", "draft": {"labels": {"g:not-a-payee": "Landlord"}}}).status_code == 422


@pytest.mark.parametrize("draft", [
    {"labels": {"row:x": "x" * 41}},
    {"paybills": {"row:x": "12ab"}},
    {"paybills": {"row:x": "123"}},
])
def test_label_and_paybill_formats_match_confirm(client, fake_user, fake_pool, monkeypatch, draft):
    patch_repo(monkeypatch, imp=import_row())
    assert client.put(url(), json={"step": "review", "draft": draft}).status_code == 422


def test_save_failure_is_a_generic_500(client, fake_user, fake_pool, monkeypatch):
    mocks = patch_repo(monkeypatch, imp=import_row())
    mocks["upsert_progress"].side_effect = RuntimeError("db down")
    response = client.put(url(), json={"step": "review", "draft": {}})
    assert response.status_code == 500 and "db down" not in response.text


# --- DELETE --------------------------------------------------------------------------


def test_discard_deletes_the_draft_only(client, fake_user, fake_pool, monkeypatch):
    mocks = patch_repo(monkeypatch, imp=import_row())
    assert client.delete(url()).status_code == 204
    mocks["delete_progress"].assert_awaited_once()
    assert mocks["delete_progress"].call_args.kwargs["import_id"] == IMPORT_ID


# --- counts (pure) ----------------------------------------------------------------------


def test_counts_include_a_labelled_row_only_once_it_has_a_category():
    keys = extract_keys(ROWS, provider="M-Pesa", secret=SECRET)
    unlabelled_only = {"rows": {PAYOUT["id"]: {"unlabelled": True}}}
    with_category = {"rows": {PAYOUT["id"]: {"unlabelled": True, "new_category_name": "Side income"}}}
    assert progress_counts(ROWS, keys, unlabelled_only) == (0, 3)
    assert progress_counts(ROWS, keys, with_category) == (1, 3)
    assert progress_counts(ROWS, keys, {"rows": {}}) == (0, 2)


def test_rows_categorised_elsewhere_since_drop_out_of_the_counts():
    keys = extract_keys(ROWS, provider="M-Pesa", secret=SECRET)
    draft = {"rows": {DONE["id"]: {"category_id": GROCERIES["id"]}, NAIVAS["id"]: {"category_id": GROCERIES["id"]}}}
    assert progress_counts(ROWS, keys, draft) == (1, 2)
