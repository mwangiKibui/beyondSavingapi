"""ab-288: a sub-ledger's untouched rows follow its new role."""

from decimal import Decimal
from unittest.mock import AsyncMock
from uuid import uuid4

import pytest

from app.services import import_handling

USER, SUB = uuid4(), uuid4()


def ledger_row(handled_as=None, role="locked_saving", direction="in"):
    return {"id": uuid4(), "account_id": uuid4(), "description": "Deposit", "direction": direction,
            "amount": Decimal("3000"), "currency": "KES", "handled_as": handled_as, "sub_ledger_role": role,
            "provider": "Stima Sacco"}


@pytest.fixture
def db(monkeypatch):
    mocks = {name: AsyncMock() for name in (
        "get_rehandle_rows", "delete_allocations", "set_handled", "insert_handled_allocations")}
    mocks["get_owned_providers"] = AsyncMock(return_value=[])
    mocks["create_categories"] = AsyncMock(return_value=[])
    for name, mock in mocks.items():
        monkeypatch.setattr(import_handling, name, mock)
    return mocks


async def test_an_untouched_deposit_becomes_saving(db, monkeypatch):
    r = ledger_row()
    monkeypatch.setattr(import_handling, "_classify",
                        lambda row, **kw: import_handling.Handling(kind="saving", reason="Saved"))
    db["get_rehandle_rows"].return_value = [r]
    assert await import_handling.rehandle_sub_ledger(object(), user_id=USER, sub_ledger_id=SUB, secret="s") == 1
    assert db["delete_allocations"].await_args.kwargs == {"transaction_ids": [r["id"]]}
    kinds = [c.kwargs["kinds"] for c in db["set_handled"].await_args_list]
    assert kinds == [[None], ["saving"]]
    [alloc] = db["insert_handled_allocations"].await_args.kwargs["allocations"]
    assert (alloc["transaction_id"], alloc["category_id"], alloc["amount"]) == (r["id"], None, 3000.0)


async def test_rows_already_handled_the_same_way_are_left(db, monkeypatch):
    monkeypatch.setattr(import_handling, "_classify",
                        lambda row, **kw: import_handling.Handling(kind="saving", reason="Saved"))
    db["get_rehandle_rows"].return_value = [ledger_row(handled_as="saving")]
    assert await import_handling.rehandle_sub_ledger(object(), user_id=USER, sub_ledger_id=SUB, secret="s") == 0
    db["set_handled"].assert_not_awaited()


async def test_saving_rows_go_back_to_the_review_when_the_role_is_spending(db, monkeypatch):
    r = ledger_row(handled_as="saving", role="spending")
    monkeypatch.setattr(import_handling, "_classify", lambda row, **kw: None)
    db["get_rehandle_rows"].return_value = [r]
    assert await import_handling.rehandle_sub_ledger(object(), user_id=USER, sub_ledger_id=SUB, secret="s") == 1
    assert db["set_handled"].await_args.kwargs["kinds"] == [None]
    db["insert_handled_allocations"].assert_not_awaited()
