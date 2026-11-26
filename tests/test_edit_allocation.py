# ruff: noqa: F811 - test arguments named db are the imported fixture
"""ab-340: editing how one transaction is allocated, from anywhere - the
service (replace, unpair, handled rows, pairing again) with repositories
mocked, and PUT /transactions/{id}/allocation."""

from contextlib import asynccontextmanager
from unittest.mock import AsyncMock
from uuid import uuid4

import pytest
from fastapi.testclient import TestClient

import app.services.category_applies as svc
from app.core.db import get_pool
from app.core.security import get_current_user_id
from app.main import app
from app.services.allocations import AllocationRejected
from app.services.category_applies import ApplyGroup, ApplyResult, edit_allocation
from tests.test_category_applies import GROCER, GROCERIES, PERSON, SALARY, SECRET, USER, row
from tests.test_category_applies import db  # noqa: F401 - the shared fixture; pytest finds it by name

BETWEEN = {"id": uuid4(), "name": "Between my accounts", "direction": None}
MINE = {"id": uuid4(), "nickname": "M-Pesa", "account_type": "mobile_money", "provider": "M-Pesa",
        "currency": "KES"}


@pytest.fixture(autouse=True)
def reasons(db):
    db["get_user_transfer_reasons"].return_value = [BETWEEN]


@pytest.fixture
def undo(monkeypatch):
    mock = AsyncMock(return_value=[])
    monkeypatch.setattr(svc, "undo_handling", mock)
    return mock


async def edit(db, rows, group):
    """rows: what each lock returns, in turn (the last one repeats)."""
    calls = iter(rows)
    last = {}

    async def lock(conn, **kw):
        last["rows"] = next(calls, last.get("rows"))
        return last["rows"]

    db["lock_rows_for_apply"].side_effect = lock
    first = rows[0][0]["id"] if rows[0] else uuid4()
    return await edit_allocation(object(), user_id=USER, transaction_id=first, group=group, secret=SECRET)


# --- the service -----------------------------------------------------------------


async def test_a_category_row_is_refiled_replacing_its_splits(db, undo):
    r = row(GROCER, amount=5000)
    filed = {**r, "allocated": 5000}
    result, released = await edit(db, [[filed], [r]], ApplyGroup(transaction_ids=[], category_id=GROCERIES["id"]))

    db["delete_allocations"].assert_awaited_once()
    assert db["delete_allocations"].await_args.kwargs["transaction_ids"] == [r["id"]]
    alloc = db["insert_applied_allocations"].await_args.kwargs
    assert alloc["transaction_ids"] == [r["id"]] and alloc["amounts"] == [5000.0]
    assert alloc["category_id"] == GROCERIES["id"]
    assert released == [] and result.groups[0]["category_name"] == "Groceries"
    undo.assert_not_awaited()
    db["unpair_hand_filed"].assert_not_awaited()


async def test_only_the_given_transaction_is_filed_whatever_the_body_says(db, undo):
    r = row(GROCER)
    await edit(db, [[r]], ApplyGroup(transaction_ids=[uuid4(), uuid4()], category_id=GROCERIES["id"]))
    assert db["lock_rows_for_apply"].await_args_list[0].kwargs["transaction_ids"] == [r["id"]]
    assert db["insert_applied_allocations"].await_args.kwargs["transaction_ids"] == [r["id"]]


async def test_a_transfer_gets_its_account_and_pairs(db, undo):
    db["get_account"].return_value = MINE
    r = row(PERSON, amount=2000)
    result, _ = await edit(db, [[{**r, "allocated": 2000}], [r]], ApplyGroup(
        transaction_ids=[], kind="transfer", transfer_reason_id=BETWEEN["id"], other_account_id=MINE["id"]))

    alloc = db["insert_applied_allocations"].await_args.kwargs
    assert alloc["transfer_reason_id"] == BETWEEN["id"] and alloc["source_account_id"] == MINE["id"]
    db["pair_hand_filed_transfers"].assert_awaited_once()
    assert result.groups[0]["other_account_id"] == MINE["id"]


async def test_a_paired_leg_is_unpaired_first_and_the_other_side_reported(db, undo):
    r = row(PERSON, amount=2000)
    other = uuid4()
    db["unpair_hand_filed"].return_value = [other]
    _, released = await edit(db, [[{**r, "allocated": 2000, "is_transfer": True}], [r]],
                             ApplyGroup(transaction_ids=[], category_id=GROCERIES["id"]))

    assert db["unpair_hand_filed"].await_args.kwargs["transaction_ids"] == [r["id"]]
    assert released == [other]
    assert db["insert_applied_allocations"].await_args.kwargs["category_id"] == GROCERIES["id"]


async def test_a_row_handled_at_import_loses_its_handling_first(db, undo):
    r = row("TRF TO M-PESA 254700000000", amount=1500)
    other = uuid4()
    undo.return_value = [r["id"], other]
    _, released = await edit(db, [[{**r, "handled_as": "transfer", "is_transfer": True}], [r]], ApplyGroup(
        transaction_ids=[], kind="transfer", transfer_reason_id=BETWEEN["id"]))

    assert undo.await_args.kwargs == {"transaction_id": r["id"], "user_id": USER, "secret": SECRET}
    db["unpair_hand_filed"].assert_not_awaited()
    assert released == [other]
    assert db["insert_applied_allocations"].await_args.kwargs["transfer_reason_id"] == BETWEEN["id"]


async def test_remember_learns_it_as_one_line_of_the_payee(db, undo):
    r = row(GROCER)
    await edit(db, [[r]], ApplyGroup(transaction_ids=[], category_id=GROCERIES["id"], remember=True))
    # per_line: the payee's latest choice, never a shared vote.
    db["upsert_merchant_votes"].assert_not_awaited()
    db["upsert_number_votes"].assert_not_awaited()


async def test_another_users_transaction_is_not_found(db, undo):
    with pytest.raises(AllocationRejected) as exc:
        await edit(db, [[]], ApplyGroup(transaction_ids=[], category_id=GROCERIES["id"]))
    assert exc.value.status_code == 404
    db["delete_allocations"].assert_not_awaited()


async def test_the_category_type_must_fit_the_direction(db, undo):
    r = row(GROCER, direction="out")
    with pytest.raises(AllocationRejected) as exc:
        await edit(db, [[r]], ApplyGroup(transaction_ids=[], category_id=SALARY["id"]))
    assert exc.value.status_code == 422


async def test_the_rows_own_account_is_refused(db, undo):
    r = row(PERSON)
    db["get_account"].return_value = {**MINE, "id": r["account_id"]}
    with pytest.raises(AllocationRejected) as exc:
        await edit(db, [[r]], ApplyGroup(transaction_ids=[], transfer_reason_id=BETWEEN["id"],
                                         other_account_id=r["account_id"]))
    assert exc.value.status_code == 422


# --- PUT /transactions/{id}/allocation --------------------------------------------

TID = uuid4()
URL = f"/transactions/{TID}/allocation"
TRANSACTION = {"id": str(TID), "account_id": str(uuid4()), "txn_date": "2026-09-01", "amount": 2000.0,
               "currency": "KES", "direction": "out", "counterparty": None, "description": "TRANSFER TO M-PESA",
               "balance_after": None, "status": "reconciled", "import_id": None, "sub_ledger_id": None,
               "sub_ledger_name": None}


class FakePool:
    def acquire(self):
        @asynccontextmanager
        async def acquire():
            class Conn:
                def transaction(self):
                    @asynccontextmanager
                    async def tx():
                        yield
                    return tx()
            yield Conn()
        return acquire()


@pytest.fixture(scope="module")
def client():
    with TestClient(app) as c:
        yield c


@pytest.fixture
def user():
    user_id = uuid4()
    app.dependency_overrides[get_current_user_id] = lambda: user_id
    yield user_id
    app.dependency_overrides.pop(get_current_user_id, None)


@pytest.fixture
def pool():
    app.dependency_overrides[get_pool] = lambda: FakePool()
    yield
    app.dependency_overrides.pop(get_pool, None)


@pytest.fixture
def saved(monkeypatch):
    service = AsyncMock(return_value=(ApplyResult(groups=[], created_categories=[], plan=svc.AllocationPlan(),
                                                  transactions_by_id={}, created_accounts=[]), []))
    monkeypatch.setattr("app.api.transactions.edit_allocation", service)
    monkeypatch.setattr("app.api.transactions.get_transaction", AsyncMock(return_value=TRANSACTION))
    monkeypatch.setattr("app.api.transactions.get_allocations", AsyncMock(return_value=[]))
    monkeypatch.setattr("app.api.transactions.alerts_after_apply", AsyncMock())
    return service


def test_requires_auth(client):
    assert client.put(URL, json={"category_id": str(uuid4())}).status_code == 401


def test_503_when_the_db_is_unreachable(client, user):
    assert client.put(URL, json={"category_id": str(uuid4())}).status_code == 503


def test_needs_exactly_one_target(client, user, pool, saved):
    response = client.put(URL, json={"category_id": str(uuid4()), "transfer_reason_id": str(uuid4())})
    assert response.status_code == 422
    saved.assert_not_awaited()


def test_a_refund_cant_be_remembered(client, user, pool, saved):
    response = client.put(URL, json={"kind": "refund", "category_id": str(uuid4()), "remember": True})
    assert response.status_code == 422


def test_saves_and_returns_the_transaction_and_what_went_back_to_review(client, user, pool, saved):
    other = uuid4()
    saved.return_value = (saved.return_value[0], [other])
    account = uuid4()
    reason = uuid4()
    response = client.put(URL, json={"kind": "transfer", "transfer_reason_id": str(reason),
                                     "other_account_id": str(account), "note": " lift ", "remember": False})

    assert response.status_code == 200
    body = response.json()
    assert body["id"] == str(TID) and body["allocations"] == [] and body["released"] == [str(other)]
    kwargs = saved.await_args.kwargs
    assert kwargs["user_id"] == user and kwargs["transaction_id"] == TID
    group = kwargs["group"]
    assert group.transaction_ids == [TID] and group.transfer_reason_id == reason
    assert group.other_account_id == account and group.note == "lift"


def test_a_rejection_keeps_its_status(client, user, pool, saved):
    saved.side_effect = AllocationRejected(404, "Transaction not found")
    response = client.put(URL, json={"category_id": str(uuid4())})
    assert response.status_code == 404 and response.json()["detail"] == "Transaction not found"


def test_an_unexpected_error_is_generic_and_logged(client, user, pool, saved, caplog):
    saved.side_effect = RuntimeError("boom: secret detail")
    response = client.put(URL, json={"category_id": str(uuid4())})
    assert response.status_code == 500 and "boom" not in response.text
    assert "editing the allocation" in caplog.text
