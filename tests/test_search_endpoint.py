"""ab-274: GET /search - the navbar search over the signed-in user's own
transactions, payees, money plans and accounts."""

from contextlib import asynccontextmanager
from datetime import date, datetime, timezone
from decimal import Decimal
from unittest.mock import AsyncMock
from uuid import uuid4

import pytest
from fastapi.testclient import TestClient

from app.core.db import get_pool
from app.core.security import get_current_user_id
from app.main import app

API = "app.api.search"
GROCER = "Merchant Payment to 5555555 - ACME SUPERMARKET"
PERSON = "Customer Transfer to 0700000000 - A N OTHER"


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


def txn(user_id=None, description=GROCER, amount="2600.00"):
    return {"id": uuid4(), "account_id": uuid4(), "account_nickname": "My M-Pesa", "txn_date": date(2026, 11, 3),
            "amount": Decimal(amount), "currency": "KES", "direction": "out", "description": description,
            "counterparty": None, "user_id": user_id}


def payee_row(description=GROCER, user_id=None):
    return {"id": uuid4(), "txn_date": date(2026, 11, 3), "amount": Decimal("100"), "currency": "KES",
            "direction": "out", "description": description, "provider": "M-Pesa", "user_id": user_id}


@pytest.fixture
def repos(monkeypatch):
    mocks = {
        "search_transactions": AsyncMock(return_value=([], 0)),
        "search_payee_rows": AsyncMock(return_value=[]),
        "search_money_plans": AsyncMock(return_value=([], 0)),
        "search_accounts": AsyncMock(return_value=([], 0)),
    }
    for name, mock in mocks.items():
        monkeypatch.setattr(f"{API}.{name}", mock)
    return mocks


# --- validation -----------------------------------------------------------------

def test_requires_auth(client):
    assert client.get("/search?q=acme").status_code == 401


@pytest.mark.parametrize("query", ["", "q=", "q=a", "q=%20a%20%20", "q=" + "x" * 101, "q=acme&limit=0",
                                   "q=acme&limit=21", "q=acme&type=robots", "q=acme&type=payees&page_size=7",
                                   "q=acme&type=payees&page=0"])
def test_rejects_bad_params(client, fake_user, fake_pool, repos, query):
    assert client.get(f"/search?{query}").status_code == 422
    assert not any(m.await_count for m in repos.values())


def test_503_without_db(client, fake_user):
    assert client.get("/search?q=acme").status_code == 503


def test_errors_are_generic(client, fake_user, fake_pool, repos):
    repos["search_transactions"].side_effect = RuntimeError("db secret")
    response = client.get("/search?q=acme")
    assert response.status_code == 500 and "secret" not in response.text


# --- results --------------------------------------------------------------------

def test_empty_results_have_every_group(client, fake_user, fake_pool, repos):
    body = client.get("/search?q=nothing").json()
    assert body["q"] == "nothing"
    assert body["groups"] == {name: {"items": [], "total": 0}
                              for name in ("transactions", "payees", "plans", "loans", "accounts")}


def test_query_is_trimmed_and_passed_as_an_escaped_pattern(client, fake_user, fake_pool, repos):
    client.get("/search?q=%20%2050%25%20%20off%20")
    kwargs = repos["search_transactions"].await_args.kwargs
    assert kwargs["pattern"] == "%50\\%%off%"
    assert kwargs["amount"] is None
    assert (kwargs["limit"], kwargs["offset"]) == (5, 0)
    assert repos["search_payee_rows"].await_args.kwargs["pattern"] == "%50\\%%off%"


@pytest.mark.parametrize("q", ["2600", "2,600", "KES 2,600"])
def test_amount_with_or_without_separators(client, fake_user, fake_pool, repos, q):
    client.get("/search", params={"q": q})
    assert repos["search_transactions"].await_args.kwargs["amount"] == Decimal("2600")


def test_every_group_type_maps_and_reports_its_total(client, fake_user, fake_pool, repos):
    plan_id, account_id = uuid4(), uuid4()
    repos["search_transactions"].return_value = ([txn()], 12)
    repos["search_payee_rows"].return_value = [payee_row(), payee_row()]
    repos["search_money_plans"].return_value = ([{"id": plan_id, "name": "Acme month", "period": "monthly",
                                                  "status": "draft",
                                                  "starts_at": datetime(2026, 11, 1, tzinfo=timezone.utc)}], 1)
    repos["search_accounts"].return_value = ([{"id": account_id, "nickname": "Acme", "provider": "M-Pesa",
                                               "account_type": "mobile_money", "is_active": True}], 1)

    groups = client.get("/search?q=acme").json()["groups"]

    assert groups["transactions"]["total"] == 12
    assert groups["transactions"]["items"][0]["subtitle"] == "3 Nov 2026 · My M-Pesa · −KES 2,600.00"
    assert groups["transactions"]["items"][0]["account_nickname"] == "My M-Pesa"
    assert groups["payees"]["total"] == 1
    assert groups["payees"]["items"][0]["row_count"] == 2
    assert groups["payees"]["items"][0]["link"]["search"] == "ACME SUPERMARKET"
    assert groups["plans"]["items"][0]["link"] == {"plan_id": str(plan_id)}
    assert groups["accounts"]["items"][0]["link"] == {"account_id": str(account_id)}
    assert groups["loans"] == {"items": [], "total": 0}


def test_groups_are_capped_at_limit(client, fake_user, fake_pool, repos):
    repos["search_payee_rows"].return_value = [
        payee_row(f"Merchant Payment to 55555{i:02d} - SHOP {chr(65 + i)} LTD") for i in range(8)
    ]
    body = client.get("/search?q=shop&limit=3").json()
    assert len(body["groups"]["payees"]["items"]) == 3
    assert body["groups"]["payees"]["total"] == 8
    assert repos["search_transactions"].await_args.kwargs["limit"] == 3


def test_one_type_is_paged_for_the_full_results_page(client, fake_user, fake_pool, repos):
    repos["search_transactions"].return_value = ([txn()], 31)

    body = client.get("/search?q=acme&type=transactions&page=3&page_size=10").json()

    kwargs = repos["search_transactions"].await_args.kwargs
    assert (kwargs["limit"], kwargs["offset"]) == (10, 20)
    assert list(body["groups"]) == ["transactions"]
    assert body["groups"]["transactions"]["total"] == 31
    for name in ("search_payee_rows", "search_money_plans", "search_accounts"):
        assert repos[name].await_count == 0


def test_loans_type_is_always_empty(client, fake_user, fake_pool, repos):
    body = client.get("/search?q=acme&type=loans").json()
    assert body["groups"] == {"loans": {"items": [], "total": 0}}
    assert not any(m.await_count for m in repos.values())


# --- privacy ----------------------------------------------------------------------

def test_every_query_is_scoped_to_the_signed_in_user(client, fake_user, fake_pool, repos):
    client.get("/search?q=acme")
    for mock in repos.values():
        assert mock.await_args.kwargs["user_id"] == fake_user


def test_two_users_never_see_each_others_rows(client, fake_pool, monkeypatch):
    """A fake store holding both users' rows, filtered the way the SQL is."""
    alice, bob = uuid4(), uuid4()
    txns = [txn(alice, "ACME SUPERMARKET OCT"), txn(bob, "ACME SUPERMARKET NOV")]
    payees = [payee_row(PERSON, alice), payee_row(GROCER, bob)]

    async def search_transactions(conn, *, user_id, **_):
        mine = [t for t in txns if t["user_id"] == user_id]
        return mine, len(mine)

    async def search_payee_rows(conn, *, user_id, **_):
        return [r for r in payees if r["user_id"] == user_id]

    monkeypatch.setattr(f"{API}.search_transactions", search_transactions)
    monkeypatch.setattr(f"{API}.search_payee_rows", search_payee_rows)
    monkeypatch.setattr(f"{API}.search_money_plans", AsyncMock(return_value=([], 0)))
    monkeypatch.setattr(f"{API}.search_accounts", AsyncMock(return_value=([], 0)))

    def search_as(user_id, q):
        app.dependency_overrides[get_current_user_id] = lambda: user_id
        try:
            return client.get("/search", params={"q": q}).json()["groups"]
        finally:
            app.dependency_overrides.pop(get_current_user_id, None)

    bobs = search_as(bob, "other")
    assert [t["title"] for t in bobs["transactions"]["items"]] == ["ACME SUPERMARKET NOV"]
    # Alice's person payee never shows up for Bob.
    assert all(not p["is_person"] for p in bobs["payees"]["items"])
    assert all(PERSON not in p["title"] for p in bobs["payees"]["items"])

    alices = search_as(alice, "other")
    assert [p["title"] for p in alices["payees"]["items"]] == [PERSON]
    assert [t["title"] for t in alices["transactions"]["items"]] == ["ACME SUPERMARKET OCT"]


def test_payee_search_never_reads_the_shared_directory(client, fake_user, fake_pool, repos, monkeypatch):
    directory = AsyncMock(side_effect=AssertionError("shared directory read"))
    monkeypatch.setattr("app.services.directory_cache.get_directory", directory)
    repos["search_payee_rows"].return_value = [payee_row()]

    assert client.get("/search?q=acme").status_code == 200
    assert directory.await_count == 0
