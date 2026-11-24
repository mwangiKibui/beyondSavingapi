"""ab-284: the Transactions -> Uncategorised tab - its badge count and its
"Categorised here" list (groups the user filed from the tab, each undoable)."""

from contextlib import asynccontextmanager
from datetime import date, datetime, timezone
from unittest.mock import AsyncMock
from uuid import uuid4

import pytest
from fastapi.testclient import TestClient

import app.services.category_applies as svc
from app.core.db import get_pool
from app.core.security import get_current_user_id
from app.main import app
from app.repositories import payee_groups as repo
from app.services.category_suggestions import Directory
from app.services.description_keywords import KeywordContext
from app.services.payee_groups import select_groups

SECRET = "test-secret"
GROCERIES = {"id": uuid4(), "name": "Groceries", "type": "expense", "entry_type": None}
POWER_CAT = {"id": uuid4(), "name": "Electricity", "type": "expense", "entry_type": None}
GROCER = "Merchant Payment to 5555555 - ACME SUPERMARKET"
POWER = "Pay Bill Online to 888880 - KPLC PREPAID Acc. 01234567890"


# --- repository SQL ----------------------------------------------------------


class FakeConn:
    def __init__(self, rows=(), value=0):
        self.rows, self.value, self.calls = list(rows), value, []

    async def fetch(self, sql, *args):
        self.calls.append((sql, args))
        return self.rows

    async def fetchval(self, sql, *args):
        self.calls.append((sql, args))
        return self.value


async def test_count_wraps_the_uncategorised_query_and_is_user_scoped():
    conn, user_id = FakeConn(value=7), uuid4()

    assert await repo.count_uncategorised_rows(conn, user_id=user_id) == 7

    sql, args = conn.calls[0]
    assert "count(*)" in sql.lower() and repo._UNCATEGORISED in sql
    assert args == (user_id,)


async def test_categorised_here_reads_only_live_applies_made_outside_a_review():
    conn, user_id = FakeConn(), uuid4()

    await repo.get_categorised_here_rows(conn, user_id=user_id)

    sql, args = conn.calls[0]
    assert "ca.import_id IS NULL" in sql and "ca.undone_at IS NULL" in sql
    assert "ca.user_id = $1" in sql and "a.user_id = $1" in sql
    assert "ORDER BY ca.created_at DESC" in sql
    assert args == (user_id,)


# --- service -----------------------------------------------------------------


def filed(description, apply_id, applied_at, *, category=GROCERIES, txn_date=date(2026, 10, 1)):
    return {"id": uuid4(), "txn_date": txn_date, "amount": 100.0, "currency": "KES", "direction": "out",
            "description": description, "provider": "M-Pesa", "import_id": uuid4(), "account_id": uuid4(),
            "file_name": "statement.pdf", "account_nickname": "Wallet", "category_id": category["id"],
            "transfer_reason_id": None, "transfer_reason_name": None, "handled_as": None, "apply_id": apply_id,
            "applied_at": applied_at}


@pytest.fixture
def db(monkeypatch):
    mocks = {
        "get_categorised_here_rows": AsyncMock(return_value=[]),
        "get_directory": AsyncMock(return_value=Directory.from_rows([], blocked=[])),
        "get_learned_names": AsyncMock(return_value={}),
        "get_user_rules": AsyncMock(return_value=[]),
        "get_user_categories": AsyncMock(return_value=[GROCERIES, POWER_CAT]),
        "get_shared_votes": AsyncMock(return_value={}),
        "get_user_transfer_reasons": AsyncMock(return_value=[]),
        "get_owner": AsyncMock(return_value={}),
        "load_keyword_context": AsyncMock(return_value=KeywordContext()),
    }
    for name, mock in mocks.items():
        monkeypatch.setattr(svc, name, mock)
    return mocks


async def test_categorised_here_is_one_group_per_apply_newest_first(db):
    old, new = uuid4(), uuid4()
    t_old = datetime(2026, 11, 1, tzinfo=timezone.utc)
    t_new = datetime(2026, 11, 2, tzinfo=timezone.utc)
    db["get_categorised_here_rows"].return_value = [
        filed(POWER, new, t_new, category=POWER_CAT),
        filed(GROCER, old, t_old, txn_date=date(2026, 9, 1)),
        filed(GROCER, old, t_old),
    ]
    user_id = uuid4()

    groups = await svc.categorised_here_groups(object(), user_id=user_id, secret=SECRET)

    assert db["get_categorised_here_rows"].await_args.kwargs["user_id"] == user_id
    assert [g["apply_id"] for g in groups] == [new, old]
    assert [g["row_count"] for g in groups] == [1, 2]
    assert groups[0]["category_name"] == "Electricity" and groups[1]["category_name"] == "Groceries"
    assert groups[0]["applied_at"] == t_new


async def test_the_same_payee_filed_twice_is_two_undoable_groups(db):
    first, second = uuid4(), uuid4()
    db["get_categorised_here_rows"].return_value = [
        filed(GROCER, second, datetime(2026, 11, 2, tzinfo=timezone.utc)),
        filed(GROCER, first, datetime(2026, 11, 1, tzinfo=timezone.utc)),
    ]

    groups = await svc.categorised_here_groups(object(), user_id=uuid4(), secret=SECRET)

    assert [g["apply_id"] for g in groups] == [second, first]
    assert len({g["group_key"] for g in groups}) == 2


async def test_nothing_filed_here_is_an_empty_list(db):
    assert await svc.categorised_here_groups(object(), user_id=uuid4(), secret=SECRET) == []
    db["get_directory"].assert_not_awaited()


def test_recent_sort_puts_the_newest_first():
    older = {"group_key": "a", "direction": "out", "is_person": False, "row_count": 9, "total": 1.0,
             "display_name": "A", "applied_at": datetime(2026, 11, 1, tzinfo=timezone.utc),
             "last_date": date(2026, 10, 1)}
    newer = {**older, "group_key": "b", "row_count": 1, "applied_at": datetime(2026, 11, 2, tzinfo=timezone.utc)}
    items, _ = select_groups([older, newer], sort="recent", order="desc")
    assert [g["group_key"] for g in items] == ["b", "a"]


# --- endpoints ---------------------------------------------------------------


class FakePool:
    def acquire(self):
        @asynccontextmanager
        async def acquire():
            yield object()
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


UNCAT = "app.api.uncategorised"


def group(key, *, apply_id=None, applied_at=None):
    return {"group_key": key, "display_name": "ACME SUPERMARKET", "label": None, "key_kind": "till",
            "number_kind": "till", "number": "5555555", "is_person": False, "is_business": True,
            "direction": "out", "currency": "KES", "row_count": 1, "total": 100.0,
            "first_date": date(2026, 10, 1), "last_date": date(2026, 10, 1), "statements": [],
            "suggestion": None, "category_id": None, "category_name": None, "apply_id": apply_id,
            "applied_at": applied_at, "handled_as": None, "handled_reason": None,
            "rows": [{"transaction_id": uuid4(), "txn_date": date(2026, 10, 1), "amount": 100.0,
                      "currency": "KES", "description": GROCER, "import_id": None}]}


def test_count_requires_auth(client):
    assert client.get("/uncategorised/count").status_code == 401


def test_count_503_without_db(client, fake_user):
    assert client.get("/uncategorised/count").status_code == 503


def test_count_is_the_users_waiting_rows(client, fake_user, fake_pool, monkeypatch):
    count = AsyncMock(return_value=4)
    monkeypatch.setattr(f"{UNCAT}.count_uncategorised_rows", count)

    assert client.get("/uncategorised/count").json() == {"row_total": 4}
    assert count.await_args.kwargs["user_id"] == fake_user


def test_count_errors_are_generic(client, fake_user, fake_pool, monkeypatch):
    monkeypatch.setattr(f"{UNCAT}.count_uncategorised_rows", AsyncMock(side_effect=RuntimeError("db secret")))
    response = client.get("/uncategorised/count")
    assert response.status_code == 500 and "db secret" not in response.text


def test_open_is_the_default_status(client, fake_user, fake_pool, monkeypatch):
    open_groups = AsyncMock(return_value=([group("g:open")], 1))
    here = AsyncMock(return_value=[])
    monkeypatch.setattr(f"{UNCAT}.uncategorised_groups", open_groups)
    monkeypatch.setattr(f"{UNCAT}.categorised_here_groups", here)

    body = client.get("/uncategorised/groups").json()

    assert [g["group_key"] for g in body["items"]] == ["g:open"]
    here.assert_not_awaited()


def test_categorised_lists_the_users_applies_from_the_tab(client, fake_user, fake_pool, monkeypatch):
    a, b = uuid4(), uuid4()
    here = AsyncMock(return_value=[
        group("g:1", apply_id=a, applied_at=datetime(2026, 11, 2, tzinfo=timezone.utc)),
        group("g:2", apply_id=b, applied_at=datetime(2026, 11, 1, tzinfo=timezone.utc)),
    ])
    open_groups = AsyncMock(return_value=([], 3))
    monkeypatch.setattr(f"{UNCAT}.categorised_here_groups", here)
    monkeypatch.setattr(f"{UNCAT}.uncategorised_groups", open_groups)
    monkeypatch.setattr(f"{UNCAT}.count_uncategorised_rows", AsyncMock(return_value=3))

    body = client.get("/uncategorised/groups?status=categorised&sort=recent").json()

    assert here.await_args.kwargs["user_id"] == fake_user
    assert [g["apply_id"] for g in body["items"]] == [str(a), str(b)]
    assert (body["total"], body["row_total"]) == (2, 3)
    open_groups.assert_not_awaited()


def test_an_undone_apply_is_gone_from_categorised(client, fake_user, fake_pool, monkeypatch):
    # The repository only reads live applies (undone_at IS NULL, checked
    # above); once undone the service returns nothing for it.
    monkeypatch.setattr(f"{UNCAT}.categorised_here_groups", AsyncMock(return_value=[]))
    monkeypatch.setattr(f"{UNCAT}.count_uncategorised_rows", AsyncMock(return_value=1))
    body = client.get("/uncategorised/groups?status=categorised").json()
    assert (body["items"], body["total"]) == ([], 0)


def test_status_is_validated(client, fake_user, fake_pool, monkeypatch):
    monkeypatch.setattr(f"{UNCAT}.uncategorised_groups", AsyncMock(return_value=([], 0)))
    assert client.get("/uncategorised/groups?status=decided").status_code == 422


def test_categorised_errors_are_generic(client, fake_user, fake_pool, monkeypatch):
    monkeypatch.setattr(f"{UNCAT}.categorised_here_groups", AsyncMock(side_effect=RuntimeError("db secret")))
    response = client.get("/uncategorised/groups?status=categorised")
    assert response.status_code == 500 and "db secret" not in response.text


def test_open_groups_say_how_many_have_a_suggestion(client, fake_user, fake_pool, monkeypatch):
    suggested = {**group("g:1"), "suggestion": {"category_id": None, "category_name": "Groceries",
                                                "is_new_category": True, "source": "directory"}}
    line_only = group("g:2")
    line_only["rows"][0]["suggestion"] = suggested["suggestion"]
    monkeypatch.setattr(f"{UNCAT}.uncategorised_groups",
                        AsyncMock(return_value=([suggested, line_only, group("g:3")], 3)))

    body = client.get("/uncategorised/groups?direction=in").json()

    # Whatever the filters - "Use all N suggestions".
    assert body["total"] == 0 and body["suggested"] == 2
