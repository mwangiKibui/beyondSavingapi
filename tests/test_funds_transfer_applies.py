# ruff: noqa: F811 - test arguments named db are the imported fixture
"""ab-285: filing payee groups as a Funds transfer - reason, the other
side (my account / a new account / someone else), private remember, undo
and earlier rows. Repositories are mocked (the db fixture); SQL is checked
live."""

from uuid import uuid4

import pytest

from app.repositories.accounts import DuplicateAccount
from app.services.allocations import AllocationRejected
from app.services.category_applies import ApplyGroup, apply_earlier
from tests.test_category_applies import PERSON, SALARY, USER, row, run
from tests.test_category_applies import db  # noqa: F401 - the shared fixture; pytest finds it by name

FAMILY_IN = {"id": uuid4(), "name": "From family", "direction": "in"}
SAVINGS_OUT = {"id": uuid4(), "name": "Savings Contribution", "direction": "out"}
BETWEEN = {"id": uuid4(), "name": "Between my accounts", "direction": None}
PESALINK_IN = "PESALINK FROM ACME LTD REF 123"
SENDER = "Funds received from 0700000000 - JANE DOE"


@pytest.fixture(autouse=True)
def reasons(db):
    db["get_user_transfer_reasons"].return_value = [FAMILY_IN, SAVINGS_OUT, BETWEEN]


def account(account_id=None):
    return {"id": account_id or uuid4(), "nickname": "Savings", "account_type": "bank", "provider": "Equity Bank",
            "currency": "KES"}


# --- filing -------------------------------------------------------------------


async def test_a_transfer_files_the_reason_with_no_category_and_no_budget_alerts(db):
    a, b = row(SENDER, direction="in", amount=2000), row(SENDER, direction="in", amount=3000)
    result = await run(db, [a, b], [ApplyGroup(transaction_ids=[a["id"], b["id"]],
                                               transfer_reason_id=FAMILY_IN["id"])])

    alloc = db["insert_applied_allocations"].await_args.kwargs
    assert alloc["category_id"] is None and alloc["transfer_reason_id"] == FAMILY_IN["id"]
    assert alloc["source_account_id"] is None and alloc["source_description"] is None
    applied = db["insert_apply"].await_args.kwargs
    assert applied["category_id"] is None and applied["transfer_reason_id"] == FAMILY_IN["id"]
    group = result.groups[0]
    assert group["category_id"] is None and group["category_name"] is None
    assert (group["transfer_reason_id"], group["transfer_reason_name"]) == (FAMILY_IN["id"], "From family")
    # Never income or spending: nothing for budget alerts, rows still filed.
    assert result.plan.category_ids == [] and result.plan.updated == [a["id"], b["id"]]


async def test_a_reason_for_the_other_direction_is_rejected(db):
    a = row(SENDER, direction="in")
    with pytest.raises(AllocationRejected) as exc:
        await run(db, [a], [ApplyGroup(transaction_ids=[a["id"]], transfer_reason_id=SAVINGS_OUT["id"])])
    assert exc.value.status_code == 422
    db["insert_apply"].assert_not_awaited()


async def test_a_reason_for_either_direction_fits_both(db):
    a, b = row(SENDER, direction="in"), row(PERSON, direction="out")
    await run(db, [a, b], [ApplyGroup(transaction_ids=[a["id"]], transfer_reason_id=BETWEEN["id"]),
                           ApplyGroup(transaction_ids=[b["id"]], transfer_reason_id=BETWEEN["id"])])
    assert db["insert_apply"].await_count == 2


async def test_another_users_reason_is_not_found(db):
    a = row(SENDER, direction="in")
    with pytest.raises(AllocationRejected) as exc:
        await run(db, [a], [ApplyGroup(transaction_ids=[a["id"]], transfer_reason_id=uuid4())])
    assert exc.value.status_code == 404


# --- the other side -------------------------------------------------------------


async def test_from_one_of_my_accounts(db):
    mine = account()
    db["get_account"].return_value = mine
    a = row(SENDER, direction="in")
    result = await run(db, [a], [ApplyGroup(transaction_ids=[a["id"]], transfer_reason_id=BETWEEN["id"],
                                            other_account_id=mine["id"])])

    assert db["get_account"].await_args.kwargs == {"account_id": mine["id"], "user_id": USER}
    assert db["insert_applied_allocations"].await_args.kwargs["source_account_id"] == mine["id"]
    assert result.groups[0]["other_account_id"] == mine["id"]


async def test_another_users_account_is_rejected(db):
    """Two-user isolation: get_account is scoped to the user, so someone
    else's account comes back as None."""
    a = row(SENDER, direction="in")
    with pytest.raises(AllocationRejected) as exc:
        await run(db, [a], [ApplyGroup(transaction_ids=[a["id"]], transfer_reason_id=BETWEEN["id"],
                                       other_account_id=uuid4())])
    assert exc.value.status_code == 422
    db["insert_apply"].assert_not_awaited()


async def test_the_rows_own_account_is_rejected(db):
    a = row(SENDER, direction="in")
    db["get_account"].return_value = account(a["account_id"])
    with pytest.raises(AllocationRejected) as exc:
        await run(db, [a], [ApplyGroup(transaction_ids=[a["id"]], transfer_reason_id=BETWEEN["id"],
                                       other_account_id=a["account_id"])])
    assert exc.value.status_code == 422


async def test_an_account_added_from_the_drawer_is_created_and_linked(db):
    new = account()
    db["create_account"].return_value = new
    a = row(SENDER, direction="in", currency="USD")
    result = await run(db, [a], [ApplyGroup(
        transaction_ids=[a["id"]], transfer_reason_id=BETWEEN["id"],
        new_account={"nickname": "Savings", "account_type": "bank", "provider": "Equity Bank",
                     "account_number": "", "currency": None},
    )])

    created = db["create_account"].await_args.kwargs
    assert created["user_id"] == USER and created["currency"] == "USD" and created["account_number"] == ""
    assert db["insert_applied_allocations"].await_args.kwargs["source_account_id"] == new["id"]
    assert result.created_accounts == [new]


async def test_a_duplicate_new_account_is_a_conflict(db):
    db["create_account"].side_effect = DuplicateAccount("123")
    a = row(SENDER, direction="in")
    with pytest.raises(AllocationRejected) as exc:
        await run(db, [a], [ApplyGroup(
            transaction_ids=[a["id"]], transfer_reason_id=BETWEEN["id"],
            new_account={"nickname": "S", "account_type": "bank", "provider": "Equity Bank",
                         "account_number": "123"},
        )])
    assert exc.value.status_code == 409


async def test_someone_else_keeps_the_private_name_on_the_rows_only(db):
    a = row(SENDER, direction="in")
    await run(db, [a], [ApplyGroup(transaction_ids=[a["id"]], transfer_reason_id=FAMILY_IN["id"],
                                   someone_else_name="Mum", remember=True)])
    assert db["insert_applied_allocations"].await_args.kwargs["source_description"] == "Mum"
    prefs = db["set_preferred_rules"].await_args.kwargs["preferences"]
    assert "Mum" not in str(prefs)


async def test_a_note_is_saved_on_the_rows_and_never_learned(db):
    # ab-288: "Other" + the user's own words, for the reports.
    a = row(SENDER, direction="in")
    await run(db, [a], [ApplyGroup(transaction_ids=[a["id"]], transfer_reason_id=FAMILY_IN["id"],
                                   note="School trip refund from the class", remember=True)])
    assert db["insert_applied_allocations"].await_args.kwargs["note"] == "School trip refund from the class"
    prefs = db["set_preferred_rules"].await_args.kwargs["preferences"]
    assert "School trip" not in str(prefs)


async def test_only_one_other_side(db):
    a = row(SENDER, direction="in")
    with pytest.raises(AllocationRejected) as exc:
        await run(db, [a], [ApplyGroup(transaction_ids=[a["id"]], transfer_reason_id=BETWEEN["id"],
                                       other_account_id=uuid4(), someone_else_name="Mum")])
    assert exc.value.status_code == 422


async def test_a_category_takes_no_other_side(db):
    a = row(SENDER, direction="in")
    with pytest.raises(AllocationRejected) as exc:
        await run(db, [a], [ApplyGroup(transaction_ids=[a["id"]], category_id=SALARY["id"],
                                       someone_else_name="Employer")])
    assert exc.value.status_code == 422


# --- remember -----------------------------------------------------------------


async def test_remember_on_a_transfer_is_a_private_rule_with_no_votes(db):
    a, b = row(SENDER, direction="in", amount=1000), row(SENDER, direction="in", amount=3000)
    await run(db, [a, b], [ApplyGroup(transaction_ids=[a["id"], b["id"]], transfer_reason_id=FAMILY_IN["id"],
                                      remember=True)])

    prefs = db["set_preferred_rules"].await_args.kwargs["preferences"]
    assert len(prefs) == 1
    assert prefs[0]["transfer_reason_id"] == FAMILY_IN["id"] and prefs[0]["category_id"] is None
    assert prefs[0]["amount"] == 2000.0
    for name in ("upsert_merchant_votes", "upsert_number_votes", "save_keyword_choices", "delete_keyword_choices"):
        db[name].assert_not_awaited()
    # Snapshotted first, so undo restores the rule exactly.
    assert "rules" in db["insert_apply"].await_args.kwargs["snapshot"]


async def test_remember_on_a_business_transfer_still_casts_no_vote(db):
    a = row("Merchant Payment to 5555555 - ACME SUPERMARKET", direction="out")
    await run(db, [a], [ApplyGroup(transaction_ids=[a["id"]], transfer_reason_id=SAVINGS_OUT["id"],
                                   remember=True)])
    db["set_preferred_rules"].assert_awaited_once()
    db["upsert_merchant_votes"].assert_not_awaited()
    db["upsert_number_votes"].assert_not_awaited()


async def test_a_line_transfer_learns_softly(db):
    a = row(SENDER, direction="in")
    await run(db, [a], [ApplyGroup(transaction_ids=[a["id"]], transfer_reason_id=FAMILY_IN["id"],
                                   remember=True, per_line=True)])
    db["learn_rules"].assert_awaited_once()
    db["set_preferred_rules"].assert_not_awaited()


# --- approving a suggested transfer -------------------------------------------------


async def test_approving_a_suggested_transfer_creates_the_reason_if_needed(db, monkeypatch):
    db["get_user_transfer_reasons"].return_value = [FAMILY_IN]
    a = row(PESALINK_IN, direction="in")
    suggestion = {"kind": "transfer", "transfer_reason_id": None, "category_id": None,
                  "category_name": "Between my accounts", "is_new_category": False, "source": "keyword",
                  "merchant_name": None}
    monkeypatch.setattr("app.services.category_applies.build_preview_rows",
                        lambda rows, **kw: [{"transaction_id": r["id"], "suggestion": suggestion} for r in rows])
    result = await run(db, [a], [ApplyGroup(transaction_ids=[a["id"]], use_suggestion=True,
                                            someone_else_name=None)])

    assert db["get_or_create_transfer_reason"].await_args.kwargs["name"] == "Between my accounts"
    assert result.groups[0]["transfer_reason_name"] == "Between my accounts"
    db["create_categories"].assert_not_awaited()


# --- earlier rows -----------------------------------------------------------------


async def test_earlier_rows_follow_the_same_transfer_and_other_side(db):
    mine = account()
    db["get_account"].return_value = mine
    waiting = [row(SENDER, direction="in"), row(SENDER, direction="in")]
    db["lock_apply"].return_value = {"id": uuid4(), "undone_at": None, "category_id": None,
                                     "transfer_reason_id": FAMILY_IN["id"], "group_key": None, "remember": True}
    db["get_apply_source"].return_value = {"source_account_id": mine["id"], "source_description": None}
    from app.services.payee_groups import extract_row_keys, payee_group_key
    keys = extract_row_keys(waiting, secret="test-secret")
    db["lock_apply"].return_value["group_key"] = payee_group_key(keys[waiting[0]["id"]], "in", waiting[0]["id"])
    db["get_uncategorised_rows"].return_value = waiting
    db["lock_rows_for_apply"].return_value = waiting

    result = await apply_earlier(object(), user_id=USER, apply_id=uuid4(), secret="test-secret")

    alloc = db["insert_applied_allocations"].await_args.kwargs
    assert alloc["transfer_reason_id"] == FAMILY_IN["id"] and alloc["source_account_id"] == mine["id"]
    assert result.groups[0]["remember"] is False
