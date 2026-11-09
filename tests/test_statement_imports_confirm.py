"""ab-253 PR 2: POST /statement-imports/{id}/confirm (service mocked)."""

from contextlib import asynccontextmanager
from datetime import date
from decimal import Decimal
from unittest.mock import AsyncMock
from uuid import uuid4

import pytest
from fastapi.testclient import TestClient

from app.core.db import get_pool
from app.core.security import get_current_user_id
from app.main import app
from app.services.allocations import AllocationPlan, AllocationRejected
from app.services.import_confirm import ConfirmResult

API = "app.api.statement_imports"
IMPORT_ID = uuid4()
URL = f"/statement-imports/{IMPORT_ID}/confirm"


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
def fake_pool():
    pool = FakePool()
    app.dependency_overrides[get_pool] = lambda: pool
    yield pool
    app.dependency_overrides.pop(get_pool, None)


@pytest.fixture
def fake_user():
    user_id = uuid4()
    app.dependency_overrides[get_current_user_id] = lambda: user_id
    yield user_id
    app.dependency_overrides.pop(get_current_user_id, None)


def import_row(status="parsed"):
    return {"id": IMPORT_ID, "account_id": uuid4(), "provider": "M-Pesa", "status": status,
            "period_start": date(2026, 10, 1), "period_end": date(2026, 10, 31)}


def result(allocated=(), *, plan=None, txns=None, created=()):
    return ConfirmResult(allocated=list(allocated), skipped=[], created_categories=list(created), learned=len(allocated),
                         reviewed_at="2026-11-09T10:00:00Z", plan=plan or AllocationPlan(), transactions_by_id=txns or {})


def item(**kw):
    return {"transaction_id": str(uuid4()), "category_id": str(uuid4()), **kw}


@pytest.fixture
def patched(monkeypatch):
    mocks = {"get_import_for_preview": AsyncMock(return_value=import_row()),
             "confirm_import": AsyncMock(return_value=result()),
             "generate_budget_alerts": AsyncMock()}
    for name, mock in mocks.items():
        monkeypatch.setattr(f"{API}.{name}", mock)
    return mocks


def test_confirm_requires_auth(client):
    assert client.post(URL, json={"items": [item()]}).status_code == 401


def test_confirm_returns_503_when_db_unreachable(client, fake_user):
    assert client.post(URL, json={"items": [item()]}).status_code == 503


@pytest.mark.parametrize("body", [
    {"items": []},
    {"items": [{"transaction_id": str(uuid4())}]},                                     # no category
    {"items": [item(new_category_name="Fuel")]},                                       # both
    {"items": [item(category_id=None, new_category_name="   ")]},                     # blank name
    {"items": [item(category_id=None, new_category_name="x" * 61)]},
    {"items": [item(label="x" * 41)]},
    {"items": [item(user_entered_paybill="12ab")]},
])
def test_confirm_rejects_bad_items(client, fake_user, fake_pool, patched, body):
    assert client.post(URL, json=body).status_code == 422
    patched["confirm_import"].assert_not_awaited()


def test_confirm_rejects_duplicate_rows(client, fake_user, fake_pool, patched):
    one = item()
    response = client.post(URL, json={"items": [one, {**one}]})
    assert response.status_code == 422
    assert "more than once" in response.json()["detail"]


def test_confirm_404_for_unknown_import(client, fake_user, fake_pool, patched):
    patched["get_import_for_preview"].return_value = None
    assert client.post(URL, json={"items": [item()]}).status_code == 404


def test_confirm_409_until_parsed(client, fake_user, fake_pool, patched):
    patched["get_import_for_preview"].return_value = import_row(status="pending")
    assert client.post(URL, json={"items": [item()]}).status_code == 409
    patched["confirm_import"].assert_not_awaited()


def test_confirm_happy_path_commits_and_returns_the_result(client, fake_user, fake_pool, patched):
    tid, cat = uuid4(), {"id": uuid4(), "name": "Fuel", "type": "expense"}
    patched["confirm_import"].return_value = result([tid], created=[cat])

    response = client.post(URL, json={"items": [item(category_id=None, new_category_name=" Fuel ", label=" Shell ")]})

    assert response.status_code == 200
    body = response.json()
    assert body["allocated"] == [str(tid)] and body["learned"] == 1
    assert body["created_categories"] == [{"id": str(cat["id"]), "name": "Fuel", "type": "expense"}]
    assert fake_pool.log == ["begin", "commit"]
    sent = patched["confirm_import"].call_args.kwargs["items"][0]
    assert (sent.new_category_name, sent.label, sent.category_id) == ("Fuel", "Shell", None)


def test_confirm_rejection_rolls_back(client, fake_user, fake_pool, patched):
    patched["confirm_import"].side_effect = AllocationRejected(422, "One or more rows aren't part of this statement")
    response = client.post(URL, json={"items": [item()]})
    assert response.status_code == 422
    assert response.json()["detail"] == "One or more rows aren't part of this statement"
    assert fake_pool.log == ["begin", "rollback"]


def test_confirm_unexpected_error_is_generic_500_and_rolls_back(client, fake_user, fake_pool, patched):
    patched["confirm_import"].side_effect = RuntimeError("db exploded")
    response = client.post(URL, json={"items": [item()]})
    assert response.status_code == 500 and "exploded" not in response.text
    assert fake_pool.log == ["begin", "rollback"]


def test_budget_alerts_run_after_commit_and_never_fail_the_request(client, fake_user, fake_pool, patched):
    tid, cat_id = uuid4(), uuid4()
    plan = AllocationPlan(transaction_ids=[tid], category_ids=[cat_id], amounts=[500.0], currencies=["KES"],
                          updated=[tid])
    txns = {tid: {"txn_date": date(2026, 10, 5)}}
    patched["confirm_import"].return_value = result([tid], plan=plan, txns=txns)
    patched["generate_budget_alerts"].side_effect = RuntimeError("alerts down")

    response = client.post(URL, json={"items": [item()]})

    assert response.status_code == 200
    kwargs = patched["generate_budget_alerts"].call_args.kwargs
    assert (kwargs["category_id"], kwargs["new_amount"]) == (cat_id, Decimal("500.0"))
