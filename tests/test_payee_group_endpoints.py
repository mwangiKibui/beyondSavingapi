"""ab-282 endpoints: GET /uncategorised/groups, GET /statement-imports/{id}/groups,
POST/DELETE /category-applies, POST /category-applies/{id}/earlier."""

from contextlib import asynccontextmanager
from datetime import date, datetime, timezone
from unittest.mock import AsyncMock
from uuid import uuid4

import pytest
from fastapi.testclient import TestClient

from app.core.db import get_pool
from app.core.security import get_current_user_id
from app.main import app
from app.services.allocations import AllocationPlan, AllocationRejected
from app.services.category_applies import ApplyResult

IMPORT_ID = uuid4()
GROCERIES = {"id": uuid4(), "name": "Groceries", "type": "expense"}
GROCER = "Merchant Payment to 5555555 - ACME SUPERMARKET"


class FakeConn:
    def transaction(self):
        @asynccontextmanager
        async def tx():
            yield
        return tx()


class FakePool:
    def acquire(self):
        @asynccontextmanager
        async def acquire():
            yield FakeConn()
        return acquire()


@pytest.fixture(scope="module")
def client():
    with TestClient(app) as c:
        yield c


@pytest.fixture
def fake_pool():
    app.dependency_overrides[get_pool] = lambda: FakePool()
    yield
    app.dependency_overrides.pop(get_pool, None)


@pytest.fixture
def fake_user():
    user_id = uuid4()
    app.dependency_overrides[get_current_user_id] = lambda: user_id
    yield user_id
    app.dependency_overrides.pop(get_current_user_id, None)


def group(key="g:1", *, name="ACME SUPERMARKET", rows=1, total=100.0, direction="out", is_person=False):
    return {"group_key": key, "display_name": name, "label": None, "key_kind": "till", "number_kind": "till",
            "number": "5555555", "is_person": is_person, "is_business": not is_person, "direction": direction,
            "currency": "KES", "row_count": rows, "total": total, "first_date": date(2026, 9, 1),
            "last_date": date(2026, 10, 1), "statements": [{"import_id": uuid4(), "file_name": "oct.pdf",
                                                             "account_nickname": "M-Pesa"}],
            "suggestion": None, "category_id": None, "category_name": None, "apply_id": None, "handled_as": None,
            "handled_reason": None,
            "rows": [{"transaction_id": uuid4(), "txn_date": date(2026, 10, 1), "amount": total, "currency": "KES",
                      "description": GROCER, "import_id": None}]}


# --- GET /uncategorised/groups ---------------------------------------------

UNCAT = "app.api.uncategorised"


def test_uncategorised_requires_auth(client):
    assert client.get("/uncategorised/groups").status_code == 401


def test_uncategorised_503_without_db(client, fake_user):
    assert client.get("/uncategorised/groups").status_code == 503


def test_uncategorised_lists_filters_and_pages_the_users_groups(client, fake_user, fake_pool, monkeypatch):
    groups = [group("g:a", rows=3), group("g:b", rows=1, direction="in"), group("g:c", rows=2, is_person=True)]
    load = AsyncMock(return_value=(groups, 6))
    monkeypatch.setattr(f"{UNCAT}.uncategorised_groups", load)

    body = client.get("/uncategorised/groups?direction=out&page_size=5").json()

    assert load.await_args.kwargs["user_id"] == fake_user
    assert [g["group_key"] for g in body["items"]] == ["g:a", "g:c"]
    assert (body["total"], body["page"], body["page_size"], body["row_total"]) == (2, 1, 5, 6)
    assert client.get("/uncategorised/groups?kind=person").json()["items"][0]["group_key"] == "g:c"
    assert client.get("/uncategorised/groups").json()["page_size"] == 30


def test_uncategorised_search(client, fake_user, fake_pool, monkeypatch):
    groups = [group("g:a"), group("g:b", name="KPLC PREPAID")]
    monkeypatch.setattr(f"{UNCAT}.uncategorised_groups", AsyncMock(return_value=(groups, 2)))

    body = client.get("/uncategorised/groups?q=kplc").json()

    assert [g["group_key"] for g in body["items"]] == ["g:b"]
    assert (body["total"], body["row_total"]) == (1, 2)


@pytest.mark.parametrize("query", ["page_size=7", "page=0", "sort=date", "direction=sideways", "kind=robot",
                                   "q=" + "x" * 101])
def test_uncategorised_rejects_bad_params(client, fake_user, fake_pool, monkeypatch, query):
    monkeypatch.setattr(f"{UNCAT}.uncategorised_groups", AsyncMock(return_value=([], 0)))
    assert client.get(f"/uncategorised/groups?{query}").status_code == 422


def test_uncategorised_errors_are_generic(client, fake_user, fake_pool, monkeypatch):
    monkeypatch.setattr(f"{UNCAT}.uncategorised_groups", AsyncMock(side_effect=RuntimeError("db secret")))
    response = client.get("/uncategorised/groups")
    assert response.status_code == 500 and "secret" not in response.text


# --- GET /statement-imports/{id}/groups ------------------------------------

SI = "app.api.statement_imports"


def import_row():
    return {"id": IMPORT_ID, "account_id": uuid4(), "account_nickname": "My M-Pesa", "provider": "M-Pesa",
            "file_name": "statement.pdf", "status": "parsed", "period_start": date(2026, 10, 1),
            "period_end": date(2026, 10, 31), "row_count": 3, "created_at": datetime(2026, 11, 1, tzinfo=timezone.utc),
            "handled_at": datetime(2026, 11, 1, tzinfo=timezone.utc)}


def txn(description, **extra):
    return {"id": uuid4(), "txn_date": date(2026, 10, 5), "amount": 100.0, "currency": "KES", "direction": "out",
            "description": description, "is_transfer": False, "allocated": 0, "category_id": None,
            "import_id": IMPORT_ID, "file_name": "statement.pdf", "account_nickname": "My M-Pesa", **extra}


@pytest.fixture
def review(monkeypatch):
    def patch(rows, rules=()):
        mocks = {
            "get_import_for_preview": AsyncMock(return_value=import_row()),
            "get_import_rows": AsyncMock(return_value=rows),
            "get_user_rules": AsyncMock(return_value=list(rules)),
            "get_approved_directory": AsyncMock(return_value=[]),
            "get_blocked_numbers": AsyncMock(return_value=[]),
            "get_user_categories": AsyncMock(return_value=[GROCERIES]),
            "get_shared_votes": AsyncMock(return_value={}),
            "get_learned_names": AsyncMock(return_value={}),
            "handle_import_by_id": AsyncMock(return_value=None),
        }
        for name, mock in mocks.items():
            monkeypatch.setattr(f"{SI}.{name}", mock)
        return mocks
    return patch


def test_review_groups_requires_auth(client):
    assert client.get(f"/statement-imports/{IMPORT_ID}/groups").status_code == 401


def test_review_groups_split_rows_into_tabs(client, fake_user, fake_pool, review):
    apply_id = uuid4()
    rule = {"key_kind": "till", "key_value": "5555555", "provider": None, "account_hash": None,
            "direction": "out", "category_id": GROCERIES["id"], "label": "Corner shop", "times_used": 2,
            "avg_amount": None}
    review([
        txn(GROCER), txn(GROCER),
        txn("Pay Bill Online to 888880 - KPLC PREPAID Acc. 1", allocated=100, category_id=GROCERIES["id"],
            apply_id=apply_id, applied_here=True),
        txn("Merchant Payment to 7777777 - OTHER SHOP", allocated=100, category_id=GROCERIES["id"]),
        txn("Pay Bill Charge", allocated=10, handled_as="charge", handled_reason="M-Pesa charge"),
    ], rules=[rule])

    decide = client.get(f"/statement-imports/{IMPORT_ID}/groups").json()
    assert decide["counts"] == {"decide": 1, "decided": 1, "sorted": 1, "handled": 1}
    assert decide["row_counts"] == {"decide": 2, "decided": 1, "sorted": 1, "handled": 1}
    assert decide["suggested"] == 1
    g = decide["items"][0]
    assert g["row_count"] == 2 and g["label"] == "Corner shop"
    assert g["suggestion"]["category_id"] == str(GROCERIES["id"]) and g["suggestion"]["source"] == "your_rule"
    assert g["statements"] == [{"import_id": str(IMPORT_ID), "file_name": "statement.pdf",
                                "account_nickname": "My M-Pesa"}]

    decided = client.get(f"/statement-imports/{IMPORT_ID}/groups?tab=decided").json()["items"][0]
    assert decided["apply_id"] == str(apply_id) and decided["category_name"] == "Groceries"
    assert decided["suggestion"] is None

    sorted_ = client.get(f"/statement-imports/{IMPORT_ID}/groups?tab=sorted").json()["items"][0]
    assert sorted_["apply_id"] is None and sorted_["category_name"] == "Groceries"

    handled = client.get(f"/statement-imports/{IMPORT_ID}/groups?tab=handled").json()["items"][0]
    assert handled["handled_as"] == "charge" and handled["handled_reason"] == "M-Pesa charge"


def test_review_groups_search_keeps_tab_counts(client, fake_user, fake_pool, review):
    review([txn(GROCER), txn("Pay Bill Online to 888880 - KPLC PREPAID Acc. 1")])

    body = client.get(f"/statement-imports/{IMPORT_ID}/groups?q=kplc").json()

    assert body["total"] == 1 and "KPLC" in body["items"][0]["rows"][0]["description"]
    assert body["counts"]["decide"] == 2
    assert body["suggested"] == 0


def _all_statuses_rows(apply_id):
    return [
        txn("Merchant Payment to 7777777 - OTHER SHOP", allocated=100, category_id=GROCERIES["id"]),
        txn("Pay Bill Online to 888880 - KPLC PREPAID Acc. 1", allocated=100, category_id=GROCERIES["id"],
            apply_id=apply_id, applied_here=True),
        txn("Pay Bill Charge", allocated=10, handled_as="charge", handled_reason="M-Pesa charge"),
        txn("Fuliza M-Pesa", allocated=0, handled_as="loan", handled_reason="Fuliza"),
        txn(GROCER), txn(GROCER),
    ]


def test_review_groups_all_lists_every_status_pending_first(client, fake_user, fake_pool, review):
    review(_all_statuses_rows(uuid4()))

    body = client.get(f"/statement-imports/{IMPORT_ID}/groups?tab=all").json()

    assert [g["status"] for g in body["items"]] == ["decide", "handled", "handled", "decided", "sorted"]
    assert body["total"] == 5
    assert body["row_counts"]["decide"] == 2
    # Pending groups keep their suggestions on All.
    assert body["items"][0]["row_count"] == 2


def test_review_groups_all_still_sorts_within_a_status(client, fake_user, fake_pool, review):
    review(_all_statuses_rows(uuid4()))

    items = client.get(f"/statement-imports/{IMPORT_ID}/groups?tab=all&sort=amount&order=desc").json()["items"]

    handled = [g for g in items if g["status"] == "handled"]
    assert [g["handled_as"] for g in handled] == ["charge", "loan"]  # 10 before 0
    assert items[0]["status"] == "decide"


def test_review_groups_each_tab_reports_its_status(client, fake_user, fake_pool, review):
    review(_all_statuses_rows(uuid4()))
    for tab in ("decide", "decided", "sorted", "handled"):
        items = client.get(f"/statement-imports/{IMPORT_ID}/groups?tab={tab}").json()["items"]
        assert items and {g["status"] for g in items} == {tab}


def test_review_groups_approved_is_decided_and_sorted(client, fake_user, fake_pool, review):
    review(_all_statuses_rows(uuid4()))

    body = client.get(f"/statement-imports/{IMPORT_ID}/groups?tab=approved").json()

    assert sorted(g["status"] for g in body["items"]) == ["decided", "sorted"] and body["total"] == 2


def test_review_groups_filter_by_handled_as(client, fake_user, fake_pool, review):
    review(_all_statuses_rows(uuid4()))

    body = client.get(f"/statement-imports/{IMPORT_ID}/groups?tab=handled&handled_as=loan").json()
    assert [g["handled_as"] for g in body["items"]] == ["loan"] and body["total"] == 1

    # On All it narrows to those handled rows only.
    body = client.get(f"/statement-imports/{IMPORT_ID}/groups?tab=all&handled_as=charge").json()
    assert [(g["status"], g["handled_as"]) for g in body["items"]] == [("handled", "charge")]
    # Counts stay whatever the filters.
    assert body["counts"] == {"decide": 1, "decided": 1, "sorted": 1, "handled": 2}


def test_review_groups_rejects_an_unknown_handled_as(client, fake_user, fake_pool, review):
    review([])
    assert client.get(f"/statement-imports/{IMPORT_ID}/groups?tab=all&handled_as=gift").status_code == 422


def test_review_groups_404_for_another_users_import(client, fake_user, fake_pool, review):
    review([])["get_import_for_preview"].return_value = None
    assert client.get(f"/statement-imports/{IMPORT_ID}/groups").status_code == 404


def test_review_groups_rejects_an_unknown_tab(client, fake_user, fake_pool, review):
    review([])
    assert client.get(f"/statement-imports/{IMPORT_ID}/groups?tab=later").status_code == 422


# --- POST /category-applies ---------------------------------------------------

CA = "app.api.category_applies"


def result(groups=(), created=()):
    return ApplyResult(groups=list(groups), created_categories=list(created), plan=AllocationPlan(),
                       transactions_by_id={})


def applied_group(**extra):
    return {"apply_id": uuid4(), "group_key": "g:1", "category_id": GROCERIES["id"], "category_name": "Groceries",
            "remember": True, "allocated": [uuid4()], "skipped": [], "earlier_waiting": 2, **extra}


@pytest.fixture
def applies(monkeypatch):
    mocks = {
        "apply_groups": AsyncMock(return_value=result([applied_group()])),
        "undo_apply": AsyncMock(return_value=[]),
        "apply_earlier": AsyncMock(return_value=result()),
        "get_import_for_preview": AsyncMock(return_value=import_row()),
        "generate_budget_alerts": AsyncMock(),
    }
    for name, mock in mocks.items():
        monkeypatch.setattr(f"{CA}.{name}", mock)
    return mocks


def body(**group):
    return {"groups": [{"transaction_ids": [str(uuid4())], **group}]}


def test_applies_require_auth(client):
    apply_id = uuid4()
    assert client.post("/category-applies", json=body(category_id=str(uuid4()))).status_code == 401
    assert client.delete(f"/category-applies/{apply_id}").status_code == 401
    assert client.post(f"/category-applies/{apply_id}/earlier").status_code == 401


def test_apply_503_without_db(client, fake_user):
    assert client.post("/category-applies", json=body(category_id=str(uuid4()))).status_code == 503


def test_apply_passes_groups_through_and_returns_each_apply(client, fake_user, fake_pool, applies):
    category_id = str(uuid4())
    response = client.post("/category-applies", json={"groups": [
        {"transaction_ids": [str(uuid4())], "category_id": category_id, "remember": True},
        {"transaction_ids": [str(uuid4())], "new_category_name": "  Pets ", "remember": False},
        {"transaction_ids": [str(uuid4())], "use_suggestion": True},
    ]})

    assert response.status_code == 201
    kwargs = applies["apply_groups"].await_args.kwargs
    assert kwargs["user_id"] == fake_user and kwargs["statement_import"] is None
    first, second, third = kwargs["groups"]
    assert str(first.category_id) == category_id and first.remember is True
    assert second.new_category_name == "Pets" and second.remember is False
    assert third.use_suggestion is True
    group_out = response.json()["groups"][0]
    assert group_out["earlier_waiting"] == 2 and group_out["category_name"] == "Groceries"


@pytest.mark.parametrize("payload", [
    {"groups": []},
    body(),                                                                       # no choice
    body(category_id=str(uuid4()), use_suggestion=True),                         # two choices
    body(category_id=str(uuid4()), new_category_name="Pets"),
    body(new_category_name="   "),                                               # blank = none
    body(new_category_name="x" * 61),
    {"groups": [{"transaction_ids": [], "category_id": str(uuid4())}]},
])
def test_apply_rejects_bad_bodies(client, fake_user, fake_pool, applies, payload):
    assert client.post("/category-applies", json=payload).status_code == 422
    applies["apply_groups"].assert_not_awaited()


def test_apply_from_a_review_checks_the_import_is_the_users(client, fake_user, fake_pool, applies):
    payload = {**body(category_id=str(uuid4())), "import_id": str(IMPORT_ID)}

    assert client.post("/category-applies", json=payload).status_code == 201
    assert applies["apply_groups"].await_args.kwargs["statement_import"]["id"] == IMPORT_ID
    assert applies["get_import_for_preview"].await_args.kwargs["user_id"] == fake_user

    applies["get_import_for_preview"].return_value = None
    assert client.post("/category-applies", json=payload).status_code == 404
    applies["get_import_for_preview"].return_value = {**import_row(), "status": "pending"}
    assert client.post("/category-applies", json=payload).status_code == 409


@pytest.mark.parametrize("code", [404, 409, 422])
def test_apply_rejections_pass_through(client, fake_user, fake_pool, applies, code):
    applies["apply_groups"].side_effect = AllocationRejected(code, "nope")
    response = client.post("/category-applies", json=body(category_id=str(uuid4())))
    assert response.status_code == code and response.json()["detail"] == "nope"


def test_apply_errors_are_generic(client, fake_user, fake_pool, applies):
    applies["apply_groups"].side_effect = RuntimeError("db secret")
    response = client.post("/category-applies", json=body(category_id=str(uuid4())))
    assert response.status_code == 500 and "secret" not in response.text


def test_apply_checks_budget_alerts_after_commit_and_survives_their_failure(client, fake_user, fake_pool, applies):
    tid = uuid4()
    plan = AllocationPlan(transaction_ids=[tid], category_ids=[GROCERIES["id"]], amounts=[100.0], currencies=["KES"],
                          updated=[tid])
    applies["apply_groups"].return_value = ApplyResult(
        groups=[applied_group()], created_categories=[], plan=plan,
        transactions_by_id={tid: {"txn_date": date(2026, 10, 1)}},
    )
    applies["generate_budget_alerts"].side_effect = RuntimeError("alerts down")
    assert client.post("/category-applies", json=body(category_id=str(uuid4()))).status_code == 201
    assert applies["generate_budget_alerts"].await_args.kwargs["category_id"] == GROCERIES["id"]


# --- DELETE /category-applies/{id} and POST .../earlier ------------------------


def test_undo_returns_the_reopened_rows(client, fake_user, fake_pool, applies):
    apply_id, reopened = uuid4(), [uuid4()]
    applies["undo_apply"].return_value = reopened
    response = client.delete(f"/category-applies/{apply_id}")
    assert response.status_code == 200 and response.json() == {"reopened": [str(reopened[0])]}
    kwargs = applies["undo_apply"].await_args.kwargs
    assert kwargs["apply_id"] == apply_id and kwargs["user_id"] == fake_user


@pytest.mark.parametrize("code", [404, 409])
def test_undo_rejections_pass_through(client, fake_user, fake_pool, applies, code):
    applies["undo_apply"].side_effect = AllocationRejected(code, "nope")
    assert client.delete(f"/category-applies/{uuid4()}").status_code == code


def test_earlier_files_the_waiting_rows(client, fake_user, fake_pool, applies):
    apply_id = uuid4()
    applies["apply_earlier"].return_value = result([applied_group(remember=False, earlier_waiting=0)])
    response = client.post(f"/category-applies/{apply_id}/earlier")
    assert response.status_code == 200 and len(response.json()["groups"]) == 1
    kwargs = applies["apply_earlier"].await_args.kwargs
    assert kwargs["apply_id"] == apply_id and kwargs["user_id"] == fake_user


@pytest.mark.parametrize("code", [404, 409])
def test_earlier_rejections_pass_through(client, fake_user, fake_pool, applies, code):
    applies["apply_earlier"].side_effect = AllocationRejected(code, "nope")
    assert client.post(f"/category-applies/{uuid4()}/earlier").status_code == code
