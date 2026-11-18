"""ab-288: a review apply files rows as any allocation type."""

# ruff: noqa: F811 - test arguments named db are the imported fixture
from uuid import uuid4

import pytest

from app.services.allocations import AllocationRejected
from app.services.category_applies import ApplyGroup, apply_earlier, reopen_lines, undo_apply
from app.services.payee_groups import review_tab, row_type
from tests.test_category_applies import GROCERIES, SALARY, USER, applied, db, row, run  # noqa: F401

DEPOSIT = "Deposit to Non-Withdrawable Deposits"
LOAN = {"id": uuid4(), "name": "Loan Repayment", "type": "expense", "entry_type": "loan_repayment"}


def group(r, **kw):
    return ApplyGroup(transaction_ids=[r["id"]], **kw)


@pytest.mark.parametrize("kind, direction, role, handled_as", [
    ("saving", "out", None, "saving"),
    ("saving", "in", "locked_saving", "saving"),
    ("saving", "in", "shares", "saving"),
    ("borrowed", "in", None, "loan"),
])
async def test_saving_and_borrowed_set_the_handling_with_no_category(db, kind, direction, role, handled_as):
    r = row(DEPOSIT, direction=direction, amount=3000, sub_ledger_role=role)
    result = await run(db, [r], [group(r, kind=kind)])

    assert db["set_rows_handled"].await_args.kwargs == {"transaction_ids": [r["id"]], "handled_as": handled_as}
    apply = db["insert_apply"].await_args.kwargs
    assert (apply["kind"], apply["category_id"], apply["transfer_reason_id"]) == (kind, None, None)
    alloc = db["insert_applied_allocations"].await_args.kwargs
    assert (alloc["category_id"], alloc["transfer_reason_id"], alloc["amounts"]) == (None, None, [3000.0])
    assert result.groups[0]["kind"] == kind
    assert result.plan.category_ids == []  # never a budget alert


@pytest.mark.parametrize("kind, direction, role, detail", [
    ("saving", "in", None, "Money in is saving only on a SACCO deposits or shares sub-ledger"),
    ("saving", "in", "spending", "Money in is saving only on a SACCO deposits or shares sub-ledger"),
    ("borrowed", "out", None, "That type isn't for money going this way"),
    ("refund", "out", None, "That type isn't for money going this way"),
    ("income", "out", None, "That type isn't for money going this way"),
    ("loan_repayment", "in", None, "That type isn't for money going this way"),
])
async def test_types_that_dont_fit_the_row_are_rejected(db, kind, direction, role, detail):
    r = row(DEPOSIT, direction=direction, sub_ledger_role=role)
    with pytest.raises(AllocationRejected) as exc:
        await run(db, [r], [group(r, kind=kind)])
    assert (exc.value.status_code, exc.value.detail) == (422, detail)


async def test_saving_isnt_remembered_and_takes_no_category(db):
    r = row(DEPOSIT)
    with pytest.raises(AllocationRejected, match="aren't remembered"):
        await run(db, [r], [group(r, kind="saving", remember=True)])
    with pytest.raises(AllocationRejected, match="no category"):
        await run(db, [r], [group(r, kind="saving", category_id=GROCERIES["id"])])


async def test_a_transfer_with_no_reason_files_under_between_my_accounts(db):
    r = row("Customer Transfer to 0700000000")
    result = await run(db, [r], [group(r, kind="transfer")])
    assert db["get_or_create_transfer_reason"].await_args.kwargs["name"] == "Between my accounts"
    assert result.groups[0]["kind"] == "transfer"
    db["set_rows_handled"].assert_not_awaited()


async def test_loan_repayment_needs_a_loan_repayment_category(db):
    db["get_user_categories"].return_value = [GROCERIES, SALARY, LOAN]
    r = row("Loan repayment")
    result = await run(db, [r], [group(r, kind="loan_repayment", category_id=LOAN["id"])])
    assert result.groups[0]["kind"] == "loan_repayment"
    assert db["insert_apply"].await_args.kwargs["kind"] == "loan_repayment"
    with pytest.raises(AllocationRejected, match="loan-repayment category"):
        await run(db, [r], [group(r, kind="loan_repayment", category_id=GROCERIES["id"])])


async def test_kind_is_worked_out_when_not_sent(db):
    out, money_in = row("Shop"), row("Pay", direction="in")
    result = await run(db, [out, money_in], [group(out, category_id=GROCERIES["id"]),
                                             group(money_in, category_id=SALARY["id"])])
    assert [g["kind"] for g in result.groups] == ["expense", "income"]


async def test_kind_refund_is_a_refund(db):
    r = row("Refund", direction="in")
    result = await run(db, [r], [group(r, kind="refund", category_id=GROCERIES["id"])])
    assert (result.groups[0]["kind"], result.groups[0]["is_refund"]) == ("refund", True)


@pytest.mark.parametrize("kind", ["saving", "borrowed"])
async def test_undo_clears_the_handling(db, kind):
    rows = [uuid4()]
    db["lock_apply"].return_value = applied(kind=kind, category_id=None)
    db["delete_apply_allocations"].return_value = rows
    await undo_apply(object(), user_id=USER, apply_id=uuid4())
    assert db["set_rows_handled"].await_args.kwargs == {"transaction_ids": rows, "handled_as": None}


async def test_undo_of_a_category_leaves_handling_alone(db):
    db["lock_apply"].return_value = applied(kind="expense")
    db["delete_apply_allocations"].return_value = [uuid4()]
    await undo_apply(object(), user_id=USER, apply_id=uuid4())
    db["set_rows_handled"].assert_not_awaited()


async def test_reopening_a_saving_line_clears_its_handling(db):
    line = uuid4()
    db["lock_apply"].return_value = applied(kind="saving", category_id=None)
    db["delete_apply_line_allocations"].return_value = [line]
    db["count_apply_allocations"].return_value = 1
    await reopen_lines(object(), user_id=USER, apply_id=uuid4(), transaction_ids=[line])
    assert db["set_rows_handled"].await_args.kwargs == {"transaction_ids": [line], "handled_as": None}


async def test_earlier_rows_of_a_saving_apply_are_saving_too(db):
    from app.services.payee_groups import extract_row_keys, payee_group_key

    waiting = [row(DEPOSIT, direction="out")]
    keys = extract_row_keys(waiting, secret="test-secret")
    db["lock_apply"].return_value = applied(
        kind="saving", category_id=None,
        group_key=payee_group_key(keys[waiting[0]["id"]], "out", waiting[0]["id"]))
    db["get_uncategorised_rows"].return_value = waiting
    db["lock_rows_for_apply"].return_value = waiting
    result = await apply_earlier(object(), user_id=USER, apply_id=uuid4(), secret="test-secret")
    assert result.groups[0]["kind"] == "saving"


# --- how the review reads a filed row -----------------------------------------

TYPES = {GROCERIES["id"]: "expense", SALARY["id"]: "income", LOAN["id"]: "expense"}
ENTRY = {GROCERIES["id"]: "general", SALARY["id"]: "general", LOAN["id"]: "loan_repayment"}


@pytest.mark.parametrize("fields, expected", [
    ({"direction": "out"}, None),
    ({"direction": "out", "category_id": GROCERIES["id"]}, "expense"),
    ({"direction": "in", "category_id": SALARY["id"]}, "income"),
    ({"direction": "in", "category_id": GROCERIES["id"]}, "refund"),
    ({"direction": "out", "category_id": LOAN["id"]}, "loan_repayment"),
    ({"direction": "out", "transfer_reason_id": uuid4()}, "transfer"),
    ({"direction": "in", "handled_as": "saving", "apply_id": uuid4()}, "saving"),
    ({"direction": "in", "handled_as": "loan", "apply_id": uuid4()}, "borrowed"),
])
def test_row_type(fields, expected):
    assert row_type(fields, category_types=TYPES, category_entry_types=ENTRY) == expected


def test_saving_filed_in_the_review_is_approved_not_handled():
    filed = {"handled_as": "saving", "apply_id": uuid4(), "applied_here": True, "allocated": 1,
             "is_transfer": False}
    assert review_tab(filed) == "decided"
    assert review_tab({**filed, "apply_id": None}) == "handled"
