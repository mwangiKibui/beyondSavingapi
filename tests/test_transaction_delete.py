"""ab-342: deleting a manual transaction (soft), refusing statement rows,
unpairing a paired transfer, and Undo. Repositories are mocked; the SQL is
checked live."""

from contextlib import asynccontextmanager
from unittest.mock import AsyncMock
from uuid import uuid4

import asyncpg
import pytest
from fastapi.testclient import TestClient

import app.services.transaction_delete as svc
from app.core.db import get_pool
from app.core.security import get_current_user_id
from app.main import app
from app.services.allocations import AllocationRejected
from app.services.transaction_delete import delete_manual_transaction, restore_manual_transaction

USER, SECRET = uuid4(), "test-secret"


def manual(**extra):
    return {"id": uuid4(), "import_id": None, "handled_as": None, "transfer_group_id": None, **extra}


@pytest.fixture
def db(monkeypatch):
    mocks = {
        "lock_transaction_for_delete": AsyncMock(return_value=None),
        "soft_delete_transaction": AsyncMock(),
        "lock_deleted_manual_transaction": AsyncMock(return_value=None),
        "restore_transaction": AsyncMock(),
        "undo_handling": AsyncMock(return_value=[]),
        "unpair_hand_filed": AsyncMock(return_value=[]),
        "pair_hand_filed_transfers": AsyncMock(return_value=0),
    }
    for name, mock in mocks.items():
        monkeypatch.setattr(svc, name, mock)
    return mocks


async def delete(db, row):
    db["lock_transaction_for_delete"].return_value = row
    return await delete_manual_transaction(
        object(), user_id=USER, transaction_id=row["id"] if row else uuid4(), secret=SECRET
    )


# --- the service ------------------------------------------------------------------


async def test_a_manual_entry_is_soft_deleted(db):
    row = manual()
    assert await delete(db, row) == []
    assert db["lock_transaction_for_delete"].await_args.kwargs == {"transaction_id": row["id"], "user_id": USER}
    db["soft_delete_transaction"].assert_awaited_once()
    assert db["soft_delete_transaction"].await_args.kwargs == {"transaction_id": row["id"]}
    db["undo_handling"].assert_not_awaited()
    db["unpair_hand_filed"].assert_not_awaited()


async def test_a_statement_row_is_refused(db):
    with pytest.raises(AllocationRejected) as exc:
        await delete(db, manual(import_id=uuid4()))
    assert exc.value.status_code == 409 and "delete the upload" in exc.value.detail
    db["soft_delete_transaction"].assert_not_awaited()


async def test_another_users_row_is_not_found(db):
    with pytest.raises(AllocationRejected) as exc:
        await delete(db, None)
    assert exc.value.status_code == 404
    db["soft_delete_transaction"].assert_not_awaited()


async def test_a_hand_filed_transfer_leg_is_unpaired_first(db):
    other = uuid4()
    db["unpair_hand_filed"].return_value = [other]
    row = manual(transfer_group_id=uuid4())
    assert await delete(db, row) == [other]
    assert db["unpair_hand_filed"].await_args.kwargs == {
        "user_id": USER, "transaction_ids": [row["id"]], "secret": SECRET,
    }
    db["soft_delete_transaction"].assert_awaited_once()


async def test_the_paired_side_of_someone_elses_filing_loses_its_handling_first(db):
    row = manual(handled_as="transfer", transfer_group_id=uuid4())
    other = uuid4()
    db["undo_handling"].return_value = [row["id"], other]
    assert await delete(db, row) == [other]
    db["unpair_hand_filed"].assert_not_awaited()
    db["soft_delete_transaction"].assert_awaited_once()


async def test_undo_restores_and_pairs_again(db):
    tid = uuid4()
    db["lock_deleted_manual_transaction"].return_value = {"id": tid}
    await restore_manual_transaction(object(), user_id=USER, transaction_id=tid)
    assert db["restore_transaction"].await_args.kwargs == {"transaction_id": tid}
    db["pair_hand_filed_transfers"].assert_awaited_once()


async def test_undo_of_a_row_that_isnt_a_deleted_manual_entry_is_not_found(db):
    with pytest.raises(AllocationRejected) as exc:
        await restore_manual_transaction(object(), user_id=USER, transaction_id=uuid4())
    assert exc.value.status_code == 404
    db["restore_transaction"].assert_not_awaited()


async def test_undo_after_the_same_entry_was_added_again_is_a_conflict(db):
    db["lock_deleted_manual_transaction"].return_value = {"id": uuid4()}
    db["restore_transaction"].side_effect = asyncpg.UniqueViolationError("dup")
    with pytest.raises(AllocationRejected) as exc:
        await restore_manual_transaction(object(), user_id=USER, transaction_id=uuid4())
    assert exc.value.status_code == 409


# --- the endpoints ------------------------------------------------------------------

TID = uuid4()


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
def services(monkeypatch):
    mocks = {"delete": AsyncMock(return_value=[]), "restore": AsyncMock()}
    monkeypatch.setattr("app.api.transactions.delete_manual_transaction", mocks["delete"])
    monkeypatch.setattr("app.api.transactions.restore_manual_transaction", mocks["restore"])
    return mocks


def test_delete_requires_auth(client):
    assert client.delete(f"/transactions/{TID}").status_code == 401


def test_delete_503_when_the_db_is_unreachable(client, user):
    assert client.delete(f"/transactions/{TID}").status_code == 503


def test_delete_returns_what_went_back_to_review(client, user, pool, services):
    other = uuid4()
    services["delete"].return_value = [other]
    response = client.delete(f"/transactions/{TID}")
    assert response.status_code == 200 and response.json() == {"released": [str(other)]}
    kwargs = services["delete"].await_args.kwargs
    assert kwargs["user_id"] == user and kwargs["transaction_id"] == TID


def test_delete_keeps_a_rejections_status(client, user, pool, services):
    services["delete"].side_effect = AllocationRejected(409, svc.STATEMENT_ROW)
    response = client.delete(f"/transactions/{TID}")
    assert response.status_code == 409 and response.json()["detail"] == svc.STATEMENT_ROW


def test_delete_unexpected_error_is_generic_and_logged(client, user, pool, services, caplog):
    services["delete"].side_effect = RuntimeError("boom secret")
    response = client.delete(f"/transactions/{TID}")
    assert response.status_code == 500 and "boom" not in response.text
    assert "deleting transaction" in caplog.text


def test_restore(client, user, pool, services):
    assert client.post(f"/transactions/{TID}/restore").status_code == 204
    assert services["restore"].await_args.kwargs == {"user_id": user, "transaction_id": TID}


def test_restore_not_found(client, user, pool, services):
    services["restore"].side_effect = AllocationRejected(404, "Transaction not found")
    assert client.post(f"/transactions/{TID}/restore").status_code == 404


def test_restore_requires_auth(client):
    assert client.post(f"/transactions/{TID}/restore").status_code == 401
