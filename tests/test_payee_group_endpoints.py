"""ab-282 endpoints: GET /uncategorised/groups, GET /statement-imports/{id}/groups,
POST/DELETE /category-applies, POST /category-applies/{id}/earlier."""

from contextlib import asynccontextmanager
from datetime import date, datetime, timezone
from unittest.mock import ANY, AsyncMock
from uuid import uuid4

import pytest
from fastapi.testclient import TestClient

from app.core.db import get_pool
from app.core.security import get_current_user_id
from app.main import app
from app.services.allocations import AllocationPlan, AllocationRejected
from app.services.category_applies import ApplyResult
from app.services.description_keywords import KeywordContext
from app.services.category_suggestions import Directory

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
            "get_directory": AsyncMock(return_value=Directory.from_rows([], blocked=[])),
            "get_user_categories": AsyncMock(return_value=[GROCERIES]),
            "get_shared_votes": AsyncMock(return_value={}),
            "get_user_transfer_reasons": AsyncMock(return_value=[]),
            "get_owner": AsyncMock(return_value={}),
            "load_keyword_context": AsyncMock(return_value=KeywordContext()),
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


def test_review_groups_reuse_the_built_directory(client, fake_user, fake_pool, review, monkeypatch):
    """ab-300: a second groups request reuses the payee Directory built by
    the first, so the shared list's rows are read (and its name patterns
    compiled) once while the version is unchanged."""
    import app.services.directory_cache as directory_cache

    review([txn(GROCER)])
    monkeypatch.setattr(f"{SI}.get_directory", directory_cache.get_directory)
    reads = {
        "get_directory_version": AsyncMock(return_value=(1, 1, None, 1, None, 0, None)),
        "get_approved_directory": AsyncMock(return_value=[]),
        "get_blocked_numbers": AsyncMock(return_value=[]),
    }
    for name, mock in reads.items():
        monkeypatch.setattr(f"app.services.directory_cache.{name}", mock)

    assert client.get(f"/statement-imports/{IMPORT_ID}/groups").status_code == 200
    assert client.get(f"/statement-imports/{IMPORT_ID}/groups").status_code == 200
    assert reads["get_directory_version"].await_count == 2
    assert reads["get_approved_directory"].await_count == 1
    assert reads["get_blocked_numbers"].await_count == 1


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


def test_review_groups_all_lists_every_status(client, fake_user, fake_pool, review):
    review(_all_statuses_rows(uuid4()))

    body = client.get(f"/statement-imports/{IMPORT_ID}/groups?tab=all").json()

    assert sorted(g["status"] for g in body["items"]) == ["decide", "decided", "handled", "handled", "sorted"]
    assert body["total"] == 5
    assert body["row_counts"]["decide"] == 2
    # Pending groups keep their suggestions on All.
    assert next(g for g in body["items"] if g["status"] == "decide")["row_count"] == 2


def test_review_groups_all_sorts_only_by_the_chosen_sort(client, fake_user, fake_pool, review):
    # 2026-11-18: a row the user files stays where it is on All - status
    # never reorders the list (Pending is the filter for that).
    review(_all_statuses_rows(uuid4()))

    items = client.get(f"/statement-imports/{IMPORT_ID}/groups?tab=all&sort=amount&order=desc").json()["items"]

    totals = [g["total"] for g in items]
    assert totals == sorted(totals, reverse=True)
    assert [g["status"] for g in items][0] != "decide" or totals[0] == max(totals)


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


def test_review_groups_keep_one_payee_in_one_row_across_pending_and_approved(client, fake_user, fake_pool, review):
    apply_id = uuid4()
    review([
        txn(GROCER, amount=300.0),
        txn(GROCER, amount=400.0, allocated=400, category_id=GROCERIES["id"], apply_id=apply_id, applied_here=True),
        txn("Pay Bill Charge", allocated=10, handled_as="charge", handled_reason="M-Pesa charge"),
    ])

    body = client.get(f"/statement-imports/{IMPORT_ID}/groups?tab=all").json()

    acme = next(g for g in body["items"] if g["handled_as"] is None)
    assert acme["row_count"] == 2 and acme["status"] == "decide"
    assert acme["pending_count"] == 1 and acme["approved_count"] == 1
    assert acme["category_id"] is None and acme["apply_id"] is None
    lines = {r["amount"]: r for r in acme["rows"]}
    assert lines[300.0]["status"] == "decide" and lines[300.0]["category_id"] is None
    assert lines[400.0]["status"] == "decided" and lines[400.0]["category_name"] == "Groceries"
    assert lines[400.0]["apply_id"] == str(apply_id)
    # Row counts stay per line.
    assert body["row_counts"] == {"decide": 1, "decided": 1, "sorted": 0, "handled": 1}
    # A mixed group shows under Pending and under Approved.
    for tab in ("decide", "approved"):
        items = client.get(f"/statement-imports/{IMPORT_ID}/groups?tab={tab}").json()["items"]
        assert [g["row_count"] for g in items] == [2]
    assert client.get(f"/statement-imports/{IMPORT_ID}/groups").json()["counts"]["decided"] == 1


def test_review_group_lines_carry_their_own_suggestion(client, fake_user, fake_pool, review):
    veg = {"id": uuid4(), "name": "Vegetables", "type": "expense", "entry_type": "general"}
    rules = [
        {"key_kind": "till", "key_value": "5555555", "provider": None, "account_hash": None, "direction": "out",
         "category_id": GROCERIES["id"], "label": None, "times_used": 9, "avg_amount": 6000},
        {"key_kind": "till", "key_value": "5555555", "provider": None, "account_hash": None, "direction": "out",
         "category_id": veg["id"], "label": None, "times_used": 3, "avg_amount": 400},
    ]
    mocks = review([txn(GROCER, amount=350.0), txn(GROCER, amount=6500.0), txn(GROCER, amount=7000.0)], rules=rules)
    mocks["get_user_categories"].return_value = [GROCERIES, veg]

    g = client.get(f"/statement-imports/{IMPORT_ID}/groups").json()["items"][0]

    by_amount = {r["amount"]: r["suggestion"]["category_name"] for r in g["rows"]}
    assert by_amount == {350.0: "Vegetables", 6500.0: "Groceries", 7000.0: "Groceries"}
    # The group's own suggestion is what most lines got.
    assert g["suggestion"]["category_name"] == "Groceries"


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
        "reopen_lines": AsyncMock(return_value=[]),
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


def test_apply_passes_per_line_through(client, fake_user, fake_pool, applies):
    client.post("/category-applies", json=body(category_id=str(uuid4()), remember=True, per_line=True))
    group = applies["apply_groups"].await_args.kwargs["groups"][0]
    assert group.per_line is True and group.remember is True


def test_reopen_returns_the_reopened_lines(client, fake_user, fake_pool, applies):
    apply_id, line = uuid4(), uuid4()
    applies["reopen_lines"].return_value = [line]
    response = client.post(f"/category-applies/{apply_id}/reopen", json={"transaction_ids": [str(line)]})
    assert response.status_code == 200 and response.json() == {"reopened": [str(line)]}
    kwargs = applies["reopen_lines"].await_args.kwargs
    assert kwargs == {"user_id": fake_user, "apply_id": apply_id, "transaction_ids": [line], "secret": ANY}


def test_reopen_needs_at_least_one_line(client, fake_user, fake_pool, applies):
    assert client.post(f"/category-applies/{uuid4()}/reopen", json={"transaction_ids": []}).status_code == 422


@pytest.mark.parametrize("code", [404, 409, 422])
def test_reopen_rejections_pass_through(client, fake_user, fake_pool, applies, code):
    applies["reopen_lines"].side_effect = AllocationRejected(code, "nope")
    response = client.post(f"/category-applies/{uuid4()}/reopen", json={"transaction_ids": [str(uuid4())]})
    assert response.status_code == code


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


# --- ab-285: Funds transfers ----------------------------------------------------

NEW_ACCOUNT = {"nickname": " Savings ", "account_type": "bank", "provider": "Equity Bank"}


def test_apply_passes_a_transfer_and_its_other_side_through(client, fake_user, fake_pool, applies):
    reason_id, account_id = uuid4(), uuid4()
    created = {"id": account_id, "nickname": "Savings", "account_type": "bank", "provider": "Equity Bank",
               "account_number": "", "currency": "KES"}
    applies["apply_groups"].return_value = ApplyResult(
        groups=[applied_group(category_id=None, category_name=None, transfer_reason_id=reason_id,
                              transfer_reason_name="Between my accounts", other_account_id=account_id)],
        created_categories=[], plan=AllocationPlan(), transactions_by_id={}, created_accounts=[created],
    )
    response = client.post("/category-applies", json={"groups": [
        {"transaction_ids": [str(uuid4())], "transfer_reason_id": str(reason_id), "new_account": NEW_ACCOUNT},
        {"transaction_ids": [str(uuid4())], "transfer_reason_id": str(reason_id), "someone_else_name": "  Mum "},
    ]})

    assert response.status_code == 201
    first, second = applies["apply_groups"].await_args.kwargs["groups"]
    assert first.transfer_reason_id == reason_id
    assert first.new_account == {"nickname": "Savings", "account_type": "bank", "provider": "Equity Bank",
                                 "account_number": "", "currency": None}
    assert second.someone_else_name == "Mum"
    out = response.json()
    assert out["groups"][0]["transfer_reason_name"] == "Between my accounts"
    assert out["groups"][0]["category_id"] is None
    assert out["created_accounts"][0]["id"] == str(account_id)
    assert "account_number" not in out["created_accounts"][0]


@pytest.mark.parametrize("payload", [
    body(transfer_reason_id=str(uuid4()), category_id=str(uuid4())),             # two choices
    body(transfer_reason_id=str(uuid4()), use_suggestion=True),
    body(transfer_reason_id=str(uuid4()), someone_else_name="x" * 61),
    body(transfer_reason_id=str(uuid4()), note="x" * 201),                       # ab-288
    body(transfer_reason_id=str(uuid4()), new_account={**NEW_ACCOUNT, "provider": "Some Bank"}),
    body(transfer_reason_id=str(uuid4()), new_account={**NEW_ACCOUNT, "account_number": "12AB"}),
    body(transfer_reason_id=str(uuid4()), new_account={**NEW_ACCOUNT, "account_type": "mobile_money",
                                                       "provider": "M-Pesa", "account_number": "0712"}),
    body(transfer_reason_id=str(uuid4()), new_account={**NEW_ACCOUNT, "nickname": "  "}),
])
def test_apply_rejects_bad_transfer_bodies(client, fake_user, fake_pool, applies, payload):
    assert client.post("/category-applies", json=payload).status_code == 422
    applies["apply_groups"].assert_not_awaited()



# --- ab-286: refunds ------------------------------------------------------------


def test_apply_passes_a_refund_through(client, fake_user, fake_pool, applies):
    client.post("/category-applies", json=body(category_id=str(uuid4()), refund=True))
    assert applies["apply_groups"].await_args.kwargs["groups"][0].refund is True


@pytest.mark.parametrize("payload, detail", [
    (body(refund=True, use_suggestion=True), "A refund needs the expense category it comes off"),
    (body(refund=True, category_id=str(uuid4()), remember=True), "Refunds aren't remembered"),
])
def test_apply_rejects_bad_refunds(client, fake_user, fake_pool, applies, payload, detail):
    response = client.post("/category-applies", json=payload)
    assert response.status_code == 422 and response.json()["detail"] == detail
    applies["apply_groups"].assert_not_awaited()


def test_review_lines_carry_account_sub_ledger_kind_and_other_side(client, fake_user, fake_pool, review):
    """ab-288, through the real response: what the Allocation drawer needs."""
    account, ledger, reason = uuid4(), uuid4(), uuid4()
    review([
        txn("Deposit", direction="in", account_id=account, sub_ledger_id=ledger, sub_ledger_name="Deposits",
            sub_ledger_role="spending"),
        txn("Customer Transfer to 0700000000", allocated=100, apply_id=uuid4(), applied_here=True,
            transfer_reason_id=reason, transfer_reason_name="Between my accounts",
            source_account_name="Equity Savings", account_id=account),
    ])
    body = client.get(f"/statement-imports/{IMPORT_ID}/groups?tab=decide").json()
    [line] = body["items"][0]["rows"]
    assert (line["account_id"], line["sub_ledger_id"], line["sub_ledger_name"], line["sub_ledger_role"]) == (
        str(account), str(ledger), "Deposits", "spending")
    assert body["items"][0]["sub_ledger_id"] == str(ledger)

    [filed] = client.get(f"/statement-imports/{IMPORT_ID}/groups?tab=decided").json()["items"][0]["rows"]
    assert (filed["kind"], filed["other_side"], filed["account_id"]) == ("transfer", "Equity Savings", str(account))



# --- ab-296: the Review step lists every row, unpaged ---------------------------


def test_review_lists_every_group_and_row_unpaged(client, fake_user, fake_pool, review):
    review(_all_statuses_rows(uuid4()))

    body = client.get(f"/statement-imports/{IMPORT_ID}/review").json()

    assert sorted(g["status"] for g in body["items"]) == ["decide", "decided", "handled", "handled", "sorted"]
    assert sum(len(g["rows"]) for g in body["items"]) == 6
    assert body["row_counts"]["decide"] == 2
    # Each line carries what the Allocation step shows for it.
    line = next(r for g in body["items"] if g["status"] == "decided" for r in g["rows"])
    assert line["category_name"] == "Groceries" and line["status"] == "decided"


def test_review_needs_auth_and_ownership(client, fake_user, fake_pool, review, monkeypatch):
    review([])
    monkeypatch.setattr("app.api.statement_imports.get_import_for_preview", AsyncMock(return_value=None))
    assert client.get(f"/statement-imports/{IMPORT_ID}/review").status_code == 404


def test_review_lines_say_which_upload_first_recorded_them(client, fake_user, fake_pool, review):
    """ab-296: a re-upload's rows were recorded by an earlier upload; the
    Review step says so."""
    earlier = uuid4()
    review([
        txn("Merchant Payment to 7777777 - OTHER SHOP"),
        txn("Merchant Payment to 7777778 - SHOP TWO", import_id=earlier, file_name="september.pdf",
            import_created_at=datetime(2026, 10, 1, 9, 0, tzinfo=timezone.utc)),
    ])
    lines = [r for g in client.get(f"/statement-imports/{IMPORT_ID}/review").json()["items"] for r in g["rows"]]
    by_import = {r["import_id"]: r for r in lines}
    assert by_import[str(IMPORT_ID)]["file_name"] == "statement.pdf"
    assert by_import[str(earlier)]["file_name"] == "september.pdf"
    assert by_import[str(earlier)]["imported_at"].startswith("2026-10-01T09:00:00")
