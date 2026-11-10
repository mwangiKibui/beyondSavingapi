"""ab-253 PR 3: "use this from now on" (update_rule) when a row is
categorised later, outside the import confirm."""

import logging
from unittest.mock import AsyncMock
from uuid import uuid4

import pytest
from fastapi.testclient import TestClient

from app.core.db import get_pool
from app.core.security import get_current_user_id
from app.main import app
from app.services.category_suggestions import Directory
from app.services.rule_updates import plan_preferred

SECRET = "test-secret"
FUEL, RENT = uuid4(), uuid4()


def row(description, *, provider="M-Pesa", direction="out", amount=1000.0):
    return {"id": uuid4(), "description": description, "direction": direction, "amount": amount,
            "provider": provider}


# --- plan_preferred (pure) ------------------------------------------------


def test_plans_one_preference_per_row_key():
    r = row("Merchant Payment to 6123456 - RUBIS ENERGY LTD", amount=3000)
    plan = plan_preferred([r], {r["id"]: FUEL}, secret=SECRET, directory=Directory.from_rows([], blocked=[]))
    assert plan == [{"key_kind": "till", "key_value": "6123456", "provider": None, "account_hash": None,
                     "direction": "out", "category_id": FUEL, "amount": 3000.0}]


def test_blocked_number_prefers_by_name():
    r = row("Pay Bill Online to 222222 - ECITIZEN Acc. ABC123")
    d = Directory.from_rows([], blocked=[{"number_kind": "paybill", "number": "222222"}])
    plan = plan_preferred([r], {r["id"]: RENT}, secret=SECRET, directory=d)
    assert (plan[0]["key_kind"], plan[0]["key_value"]) == ("name", "ECITIZEN")


def test_reference_keeps_its_provider_and_rows_without_a_key_are_skipped():
    ref = row("IB Outgoing Mobile Payments PayBill FTX26232ABCDE 123456", provider="NCBA Bank")
    none = row("OverDraft of Credit Party", direction="in")
    plan = plan_preferred([ref, none], {ref["id"]: RENT, none["id"]: FUEL}, secret=SECRET,
                          directory=Directory.from_rows([], blocked=[]))
    assert len(plan) == 1 and plan[0]["provider"] == "NCBA Bank" and plan[0]["key_kind"] == "reference"


def test_same_key_twice_in_one_request_keeps_the_last_choice():
    a = row("Merchant Payment to 6123456 - RUBIS ENERGY LTD")
    b = row("Merchant Payment to 6123456 - RUBIS ENERGY LTD")
    plan = plan_preferred([a, b], {a["id"]: FUEL, b["id"]: RENT}, secret=SECRET,
                          directory=Directory.from_rows([], blocked=[]))
    assert [p["category_id"] for p in plan] == [RENT]


# --- endpoints --------------------------------------------------------------

TRANSACTION_ID = uuid4()
CATEGORY_ID = uuid4()
TRANSACTION_ROW = {"id": str(TRANSACTION_ID), "account_id": str(uuid4()), "txn_date": "2026-09-01", "amount": 1000.0,
                   "currency": "KES", "direction": "out", "counterparty": None, "description": "X",
                   "balance_after": None, "status": "unreconciled", "import_id": None, "sub_ledger_id": None,
                   "sub_ledger_name": None}


@pytest.fixture(scope="module")
def client():
    with TestClient(app) as c:
        yield c


@pytest.fixture
def setup(monkeypatch):
    pool = object()
    user_id = uuid4()
    app.dependency_overrides[get_pool] = lambda: pool
    app.dependency_overrides[get_current_user_id] = lambda: user_id
    mocks = {
        "get_transaction": AsyncMock(return_value=TRANSACTION_ROW),
        "get_allocations": AsyncMock(return_value=[]),
        "get_categories_by_ids": AsyncMock(return_value=[{"id": CATEGORY_ID, "name": "Fuel", "type": "expense"}]),
        "insert_allocations": AsyncMock(),
        "generate_budget_alerts": AsyncMock(),
        "get_transactions_with_allocated": AsyncMock(return_value=[
            {"id": TRANSACTION_ID, "amount": 1000.0, "allocated": 0, "currency": "KES", "direction": "out",
             "txn_date": None}]),
        "insert_bulk_allocations": AsyncMock(),
        "remember_preferred": AsyncMock(return_value=1),
    }
    for name, mock in mocks.items():
        monkeypatch.setattr(f"app.api.transactions.{name}", mock)
    yield {"user_id": user_id, "pool": pool, **mocks}
    app.dependency_overrides.pop(get_pool, None)
    app.dependency_overrides.pop(get_current_user_id, None)


def split(category_id=CATEGORY_ID, amount=1000.0, **kw):
    return {"category_id": str(category_id) if category_id else None, "amount": amount, **kw}


def test_single_category_with_update_rule_is_remembered(client, setup):
    url = f"/transactions/{TRANSACTION_ID}/allocations"
    assert client.post(url, json={"allocations": [split()], "update_rule": True}).status_code == 201
    kwargs = setup["remember_preferred"].call_args.kwargs
    assert kwargs["user_id"] == setup["user_id"]
    assert kwargs["category_by_transaction"] == {TRANSACTION_ID: CATEGORY_ID}


def test_update_rule_defaults_to_off(client, setup):
    client.post(f"/transactions/{TRANSACTION_ID}/allocations", json={"allocations": [split()]})
    setup["remember_preferred"].assert_not_awaited()


def test_splits_and_no_category_teach_nothing(client, setup):
    url = f"/transactions/{TRANSACTION_ID}/allocations"
    other = uuid4()
    setup["get_categories_by_ids"].return_value = [{"id": CATEGORY_ID, "name": "Fuel", "type": "expense"},
                                                   {"id": other, "name": "Rent", "type": "expense"}]
    client.post(url, json={"allocations": [split(amount=500), split(other, amount=500)], "update_rule": True})
    client.post(url, json={"allocations": [split(None)], "update_rule": True})
    client.post(url, json={"allocations": [split(amount=500), split(None, amount=500)], "update_rule": True})
    setup["remember_preferred"].assert_not_awaited()


def test_two_splits_of_the_same_category_still_teach(client, setup):
    client.post(f"/transactions/{TRANSACTION_ID}/allocations",
                json={"allocations": [split(amount=400), split(amount=600)], "update_rule": True})
    setup["remember_preferred"].assert_awaited_once()


def test_a_learning_failure_never_fails_the_allocation(client, setup, caplog):
    setup["remember_preferred"].side_effect = RuntimeError("rules down")
    with caplog.at_level(logging.ERROR):
        response = client.post(f"/transactions/{TRANSACTION_ID}/allocations",
                               json={"allocations": [split()], "update_rule": True})
    assert response.status_code == 201
    assert "remembering" in caplog.text


def test_bulk_allocate_update_rule(client, setup):
    body = {"transaction_ids": [str(TRANSACTION_ID)], "category_id": str(CATEGORY_ID), "update_rule": True}
    assert client.post("/transactions/bulk-allocate", json=body).status_code == 201
    assert setup["remember_preferred"].call_args.kwargs["category_by_transaction"] == {TRANSACTION_ID: CATEGORY_ID}


def test_bulk_allocate_without_a_category_teaches_nothing(client, setup):
    body = {"transaction_ids": [str(TRANSACTION_ID)], "update_rule": True}
    client.post("/transactions/bulk-allocate", json=body)
    setup["remember_preferred"].assert_not_awaited()
