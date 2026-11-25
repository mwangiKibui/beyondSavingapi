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


def import_row(status="parsed", reviewed_at=None, error_detail=None, allocated_at=None):
    return {"id": IMPORT_ID, "error_detail": error_detail, "account_id": uuid4(), "account_nickname": "My M-Pesa", "provider": "M-Pesa",
            "file_name": "statement.pdf", "status": status, "period_start": date(2026, 10, 1),
            "period_end": date(2026, 10, 31), "row_count": 3, "reviewed_at": reviewed_at,
            "allocated_at": allocated_at,
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


NO_OVERLAP = {"added_count": 3, "already_count": 0, "earlier": None}


def patch_repo(monkeypatch, *, imp=None, progress=None, rows=ROWS, categories=(GROCERIES,), overlap=NO_OVERLAP):
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
        "get_import_overlap": AsyncMock(return_value=overlap),
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


# ab-296: Upload, Processing, Allocation, Review, Finish import.
@pytest.mark.parametrize("status,reviewed_at,allocated_at,step", [
    ("pending", None, None, "processing"),
    ("failed", None, None, "failed"),
    ("parsed", None, None, "allocation"),
    ("parsed", None, UPDATED, "review"),
    ("parsed", UPDATED, None, "done"),
    ("parsed", UPDATED, UPDATED, "done"),
])
def test_without_saved_progress_the_step_comes_from_the_import(client, fake_user, fake_pool, monkeypatch,
                                                               status, reviewed_at, allocated_at, step):
    imp = import_row(status, reviewed_at, allocated_at=allocated_at)
    patch_repo(monkeypatch, imp=imp)
    body = client.get(url()).json()
    parsed = status == "parsed"
    assert body == {"step": step, "draft": {"rows": {}, "labels": {}, "paybills": {}},
                    "reviewed_count": 0, "total_count": 0, "updated_at": None, "error_detail": None,
                    "added_count": 3 if parsed else None, "already_count": 0 if parsed else None,
                    "already_recorded_by": None, "account_id": str(imp["account_id"]), "sub_ledger_id": None,
                    "file_name": "statement.pdf"}


def test_progress_says_which_account_and_sub_ledger_the_upload_is_for(client, fake_user, fake_pool, monkeypatch):
    """ab-329: the wizard goes back there after a mapping."""
    sub_ledger_id = uuid4()
    imp = {**import_row(), "sub_ledger_id": sub_ledger_id}
    patch_repo(monkeypatch, imp=imp)
    body = client.get(url()).json()
    assert (body["account_id"], body["sub_ledger_id"]) == (str(imp["account_id"]), str(sub_ledger_id))
    patch_repo(monkeypatch, imp=imp, progress={"step": "review", "draft": {}, "reviewed_count": 0,
                                               "total_count": 0, "updated_at": UPDATED})
    body = client.get(url()).json()
    assert (body["account_id"], body["sub_ledger_id"]) == (str(imp["account_id"]), str(sub_ledger_id))
    saved = client.put(url(), json={"step": "review", "draft": {}}).json()
    assert (saved["account_id"], saved["sub_ledger_id"]) == (str(imp["account_id"]), str(sub_ledger_id))


def test_a_failed_parse_says_why(client, fake_user, fake_pool, monkeypatch):
    """ab-266: the wizard's Processing step shows the reason."""
    patch_repo(monkeypatch, imp=import_row("failed", error_detail="This file doesn't look like an M-Pesa statement."))
    body = client.get(url()).json()
    assert (body["step"], body["error_detail"]) == ("failed", "This file doesn't look like an M-Pesa statement.")


def test_a_saved_layout_that_sent_it_back_to_columns_says_why(client, fake_user, fake_pool, monkeypatch):
    why = "We couldn't read the whole statement with these columns: The balance doesn't add up at row 2"
    patch_repo(monkeypatch, imp=import_row("needs_mapping", error_detail=why))
    body = client.get(url()).json()
    assert (body["step"], body["error_detail"]) == ("layout", why)


def test_an_old_reason_is_not_shown_once_it_is_read(client, fake_user, fake_pool, monkeypatch):
    patch_repo(monkeypatch, imp=import_row("parsed", error_detail="stale"))
    assert client.get(url()).json()["error_detail"] is None


def test_saved_progress_is_returned(client, fake_user, fake_pool, monkeypatch):
    draft = {"rows": {str(NAIVAS["id"]): {"category_id": str(GROCERIES["id"])}}, "labels": {}, "paybills": {}}
    patch_repo(monkeypatch, imp=import_row(), progress={"step": "review", "draft": draft, "reviewed_count": 1,
                                                        "total_count": 2, "updated_at": UPDATED})
    body = client.get(url()).json()
    # ab-296: the import says which step it's on; an old draft only adds counts.
    assert body["step"] == "allocation" and body["reviewed_count"] == 1 and body["total_count"] == 2
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


# --- ab-296: moving between Allocation and Review -----------------------------


def step_url():
    return f"/statement-imports/{IMPORT_ID}/step"


def test_step_requires_auth(client):
    assert client.put(step_url(), json={"step": "review"}).status_code == 401


@pytest.mark.parametrize("step,allocated", [("review", True), ("allocation", False)])
def test_step_moves_between_allocation_and_review(client, fake_user, fake_pool, monkeypatch, step, allocated):
    patch_repo(monkeypatch, imp=import_row())
    monkeypatch.setattr(f"{API}._rows_left", AsyncMock(return_value=0))
    set_allocated = AsyncMock(return_value=UPDATED if allocated else None)
    monkeypatch.setattr(f"{API}.set_import_allocated", set_allocated)
    response = client.put(step_url(), json={"step": step})
    assert response.status_code == 200, response.text
    assert response.json() == {"step": step}
    assert set_allocated.await_args.kwargs == {"import_id": IMPORT_ID, "allocated": allocated}


def test_step_on_a_finished_import_reads_done(client, fake_user, fake_pool, monkeypatch):
    # "Review again" moves around freely; the import stays finished.
    patch_repo(monkeypatch, imp=import_row(reviewed_at=UPDATED))
    monkeypatch.setattr(f"{API}._rows_left", AsyncMock(return_value=0))
    monkeypatch.setattr(f"{API}.set_import_allocated", AsyncMock(return_value=UPDATED))
    assert client.put(step_url(), json={"step": "review"}).json() == {"step": "review"}


def test_ab335_review_waits_until_nothing_is_pending_or_suggested(client, fake_user, fake_pool, monkeypatch):
    patch_repo(monkeypatch, imp=import_row())
    monkeypatch.setattr(f"{API}._rows_left", AsyncMock(return_value=12))
    set_allocated = AsyncMock()
    monkeypatch.setattr(f"{API}.set_import_allocated", set_allocated)
    response = client.put(step_url(), json={"step": "review"})
    assert response.status_code == 409
    assert response.json()["detail"] == "Accept or allocate the 12 remaining rows first"
    set_allocated.assert_not_awaited()


def test_ab335_going_back_to_allocation_never_waits(client, fake_user, fake_pool, monkeypatch):
    patch_repo(monkeypatch, imp=import_row())
    left = AsyncMock(return_value=12)
    monkeypatch.setattr(f"{API}._rows_left", left)
    monkeypatch.setattr(f"{API}.set_import_allocated", AsyncMock(return_value=None))
    assert client.put(step_url(), json={"step": "allocation"}).status_code == 200
    left.assert_not_awaited()


def test_step_before_processing_finishes_is_a_conflict(client, fake_user, fake_pool, monkeypatch):
    patch_repo(monkeypatch, imp=import_row("pending"))
    set_allocated = AsyncMock()
    monkeypatch.setattr(f"{API}.set_import_allocated", set_allocated)
    assert client.put(step_url(), json={"step": "review"}).status_code == 409
    set_allocated.assert_not_awaited()


def test_step_of_another_users_import_is_not_found(client, fake_user, fake_pool, monkeypatch):
    patch_repo(monkeypatch, imp=None)
    assert client.put(step_url(), json={"step": "review"}).status_code == 404


@pytest.mark.parametrize("body", [{"step": "done"}, {"step": "processing"}, {}])
def test_step_only_takes_allocation_or_review(client, fake_user, fake_pool, monkeypatch, body):
    patch_repo(monkeypatch, imp=import_row())
    assert client.put(step_url(), json=body).status_code == 422


def test_step_failure_is_a_generic_500(client, fake_user, fake_pool, monkeypatch):
    patch_repo(monkeypatch, imp=import_row())
    monkeypatch.setattr(f"{API}.set_import_allocated", AsyncMock(side_effect=RuntimeError("db down")))
    response = client.put(step_url(), json={"step": "review"})
    assert response.status_code == 500 and "db down" not in response.text


# --- ab-297: a statement that was already uploaded ------------------------------


def test_progress_says_which_upload_already_has_the_rows(client, fake_user, fake_pool, monkeypatch):
    earlier = uuid4()
    mocks = patch_repo(monkeypatch, imp=import_row(), overlap={
        "added_count": 0, "already_count": 3,
        "earlier": {"import_id": earlier, "file_name": "september.pdf", "uploaded_at": UPDATED},
    })
    body = client.get(url()).json()
    assert (body["added_count"], body["already_count"]) == (0, 3)
    assert body["already_recorded_by"] == {"import_id": str(earlier), "file_name": "september.pdf",
                                           "uploaded_at": "2026-11-11T12:00:00Z"}
    assert mocks["get_import_overlap"].await_args.kwargs == {"import_id": IMPORT_ID}


def test_overlap_is_only_worked_out_once_parsed(client, fake_user, fake_pool, monkeypatch):
    mocks = patch_repo(monkeypatch, imp=import_row("pending"))
    assert client.get(url()).json()["added_count"] is None
    mocks["get_import_overlap"].assert_not_awaited()


def delete_url():
    return f"/statement-imports/{IMPORT_ID}"


def patch_delete(monkeypatch, *, imp, removed=0):
    patch_repo(monkeypatch, imp=imp)
    remove = AsyncMock(return_value=removed)
    monkeypatch.setattr(f"{API}.soft_delete_import", remove)
    storage_calls: list[dict] = []
    storage = type("S", (), {"delete_object": lambda self, **kw: storage_calls.append(kw)})()
    monkeypatch.setattr(f"{API}.get_storage_client", lambda: storage)
    return remove, storage_calls


def test_delete_requires_auth(client):
    assert client.delete(delete_url()).status_code == 401


def test_an_upload_that_added_nothing_can_be_removed(client, fake_user, fake_pool, monkeypatch):
    remove, _ = patch_delete(monkeypatch, imp=import_row())
    assert client.delete(delete_url()).status_code == 204
    assert remove.await_args.kwargs == {"import_id": IMPORT_ID}


def test_ab334_an_upload_with_its_own_rows_is_soft_deleted_with_them(client, fake_user, fake_pool, monkeypatch):
    remove, _ = patch_delete(monkeypatch, imp=import_row(), removed=1040)
    assert client.delete(delete_url()).status_code == 204
    remove.assert_awaited_once()


def test_ab334_the_stored_file_is_kept(client, fake_user, fake_pool, monkeypatch):
    _, storage_calls = patch_delete(monkeypatch, imp=import_row(), removed=3)
    assert client.delete(delete_url()).status_code == 204
    assert storage_calls == []


def test_an_upload_still_processing_is_kept(client, fake_user, fake_pool, monkeypatch):
    remove, _ = patch_delete(monkeypatch, imp=import_row("pending"))
    response = client.delete(delete_url())
    assert response.status_code == 409
    assert "processing" in response.json()["detail"]
    remove.assert_not_awaited()


def test_ab334_an_upload_that_starts_processing_meanwhile_is_kept(client, fake_user, fake_pool, monkeypatch):
    from app.repositories.statement_imports import ImportStillProcessing

    patch_repo(monkeypatch, imp=import_row())
    monkeypatch.setattr(f"{API}.soft_delete_import", AsyncMock(side_effect=ImportStillProcessing()))
    assert client.delete(delete_url()).status_code == 409


def test_ab334_an_upload_deleted_meanwhile_is_not_found(client, fake_user, fake_pool, monkeypatch):
    patch_delete(monkeypatch, imp=import_row(), removed=None)
    assert client.delete(delete_url()).status_code == 404


def test_someone_elses_upload_is_not_found(client, fake_user, fake_pool, monkeypatch):
    remove, _ = patch_delete(monkeypatch, imp=None)
    assert client.delete(delete_url()).status_code == 404
    remove.assert_not_awaited()


def test_delete_failure_is_a_generic_500(client, fake_user, fake_pool, monkeypatch):
    patch_repo(monkeypatch, imp=import_row())
    monkeypatch.setattr(f"{API}.soft_delete_import", AsyncMock(side_effect=RuntimeError("db down")))
    response = client.delete(delete_url())
    assert response.status_code == 500 and "db down" not in response.text