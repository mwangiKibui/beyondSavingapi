"""ab-286: money in filed as a refund of an expense category."""

# ruff: noqa: F811 - test arguments named db are the imported fixture
from uuid import uuid4

import pytest

from app.services.allocations import AllocationRejected
from app.services.category_applies import ApplyGroup, apply_earlier
from tests.test_category_applies import GROCERIES, SALARY, USER, db, row, run  # noqa: F401 - shared fixture

REFUND = "Funds received from 0700000000 - ACME SUPERMARKET REFUND"


async def test_a_refund_files_money_in_under_an_expense_category(db):
    a = row(REFUND, direction="in", amount=1000)
    result = await run(db, [a], [ApplyGroup(transaction_ids=[a["id"]], category_id=GROCERIES["id"], refund=True)])

    alloc = db["insert_applied_allocations"].await_args.kwargs
    assert alloc["category_id"] == GROCERIES["id"] and alloc["amounts"] == [1000.0]
    group = result.groups[0]
    assert group["is_refund"] is True and group["category_name"] == "Groceries"
    # A refund only lowers spending: no budget alert check.
    assert result.plan.category_ids == [] and result.plan.updated == [a["id"]]


async def test_without_refund_money_in_still_cant_take_an_expense_category(db):
    a = row(REFUND, direction="in")
    with pytest.raises(AllocationRejected) as exc:
        await run(db, [a], [ApplyGroup(transaction_ids=[a["id"]], category_id=GROCERIES["id"])])
    assert exc.value.status_code == 422


async def test_a_refund_needs_an_expense_category(db):
    a = row(REFUND, direction="in")
    with pytest.raises(AllocationRejected) as exc:
        await run(db, [a], [ApplyGroup(transaction_ids=[a["id"]], category_id=SALARY["id"], refund=True)])
    assert exc.value.status_code == 422


async def test_only_money_in_can_be_a_refund(db):
    a = row(REFUND, direction="out")
    with pytest.raises(AllocationRejected) as exc:
        await run(db, [a], [ApplyGroup(transaction_ids=[a["id"]], category_id=GROCERIES["id"], refund=True)])
    assert exc.value.status_code == 422


async def test_a_new_category_for_a_refund_is_an_expense_category(db):
    created = {"id": uuid4(), "name": "Electronics", "type": "expense"}
    db["create_categories"].return_value = [created]
    a = row(REFUND, direction="in")
    await run(db, [a], [ApplyGroup(transaction_ids=[a["id"]], new_category_name="Electronics", refund=True)])
    assert db["create_categories"].await_args.kwargs["categories"] == [("Electronics", "expense")]


async def test_a_refund_learns_nothing(db):
    a = row(REFUND, direction="in")
    await run(db, [a], [ApplyGroup(transaction_ids=[a["id"]], category_id=GROCERIES["id"], refund=True)])
    for name in ("set_preferred_rules", "learn_rules", "upsert_merchant_votes", "upsert_number_votes",
                 "save_keyword_choices"):
        db[name].assert_not_awaited()


async def test_earlier_rows_of_a_refund_stay_refunds(db):
    from app.services.payee_groups import extract_row_keys, payee_group_key

    waiting = [row(REFUND, direction="in")]
    keys = extract_row_keys(waiting, secret="test-secret")
    db["lock_apply"].return_value = {
        "id": uuid4(), "undone_at": None, "category_id": GROCERIES["id"], "category_type": "expense",
        "transfer_reason_id": None, "remember": False,
        "group_key": payee_group_key(keys[waiting[0]["id"]], "in", waiting[0]["id"]),
    }
    db["get_uncategorised_rows"].return_value = waiting
    db["lock_rows_for_apply"].return_value = waiting

    result = await apply_earlier(object(), user_id=USER, apply_id=uuid4(), secret="test-secret")
    assert result.groups[0]["is_refund"] is True
