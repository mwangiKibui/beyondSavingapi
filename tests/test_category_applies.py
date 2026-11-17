"""ab-282: applying categories to payee groups, remember on/off, undo, and
the earlier-rows follow-up. Repositories are mocked; SQL is checked live."""

from datetime import date, datetime, timezone
from unittest.mock import AsyncMock
from uuid import uuid4

import pytest

import app.services.category_applies as svc
from app.services.allocations import AllocationRejected
from app.services.category_applies import ApplyGroup, apply_earlier, apply_groups, reopen_lines, undo_apply

SECRET = "test-secret"
USER = uuid4()
GROCERIES = {"id": uuid4(), "name": "Groceries", "type": "expense"}
SALARY = {"id": uuid4(), "name": "Salary", "type": "income"}
GROCER = "Merchant Payment to 5555555 - ACME SUPERMARKET"
POWER = "Pay Bill Online to 888880 - KPLC PREPAID Acc. 01234567890"
PERSON = "Customer Transfer to 0700000000 - JANE DOE"


def row(description, *, amount=100.0, direction="out", allocated=0, **extra):
    return {"id": uuid4(), "txn_date": date(2026, 10, 1), "amount": amount, "currency": "KES",
            "direction": direction, "description": description, "provider": "M-Pesa", "import_id": uuid4(),
            "account_id": uuid4(), "allocated": allocated, "category_id": None, "is_transfer": False,
            "handled_as": None, **extra}


@pytest.fixture
def db(monkeypatch):
    """Every repository the service calls, as AsyncMocks with safe defaults."""
    mocks = {
        "lock_rows_for_apply": AsyncMock(return_value=[]),
        "get_import_rows": AsyncMock(return_value=[]),
        "get_approved_directory": AsyncMock(return_value=[]),
        "get_blocked_numbers": AsyncMock(return_value=[]),
        "get_learned_names": AsyncMock(return_value={}),
        "get_user_rules": AsyncMock(return_value=[]),
        "get_user_categories": AsyncMock(return_value=[GROCERIES, SALARY]),
        "get_shared_votes": AsyncMock(return_value={}),
        "create_categories": AsyncMock(return_value=[]),
        "insert_apply": AsyncMock(side_effect=lambda conn, **kw: uuid4()),
        "insert_applied_allocations": AsyncMock(),
        "get_rule_rows": AsyncMock(return_value=[]),
        "get_merchant_votes": AsyncMock(return_value=[]),
        "get_number_votes": AsyncMock(return_value=[]),
        "set_preferred_rules": AsyncMock(),
        "learn_rules": AsyncMock(),
        "delete_apply_line_allocations": AsyncMock(return_value=[]),
        "count_apply_allocations": AsyncMock(return_value=0),
        "upsert_merchant_votes": AsyncMock(),
        "upsert_number_votes": AsyncMock(),
        "get_uncategorised_rows": AsyncMock(return_value=[]),
        "lock_apply": AsyncMock(return_value=None),
        "delete_apply_allocations": AsyncMock(return_value=[]),
        "restore_snapshot": AsyncMock(),
        "mark_apply_undone": AsyncMock(),
    }
    for name, mock in mocks.items():
        monkeypatch.setattr(svc, name, mock)
    return mocks


async def run(db, rows, groups, **kwargs):
    db["lock_rows_for_apply"].return_value = rows
    return await apply_groups(object(), user_id=USER, groups=groups, secret=SECRET, **kwargs)


# --- bulk apply -------------------------------------------------------------


async def test_bulk_apply_two_groups_each_get_their_own_apply(db):
    a, b, c = row(GROCER, amount=150), row(GROCER, amount=250), row(POWER, amount=500)
    result = await run(db, [a, b, c], [
        ApplyGroup(transaction_ids=[a["id"], b["id"]], category_id=GROCERIES["id"]),
        ApplyGroup(transaction_ids=[c["id"]], category_id=GROCERIES["id"]),
    ])

    assert len(result.groups) == 2 and db["insert_apply"].await_count == 2
    first = result.groups[0]
    assert first["allocated"] == [a["id"], b["id"]] and first["category_name"] == "Groceries"
    assert first["group_key"] != result.groups[1]["group_key"]
    calls = db["insert_applied_allocations"].await_args_list
    assert calls[0].kwargs["apply_id"] == first["apply_id"]
    assert calls[0].kwargs["amounts"] == [150.0, 250.0]
    assert result.plan.updated == [a["id"], b["id"], c["id"]]


async def test_remember_off_learns_nothing(db):
    a = row(GROCER)
    result = await run(db, [a], [ApplyGroup(transaction_ids=[a["id"]], category_id=GROCERIES["id"])])

    assert db["insert_apply"].await_args.kwargs["remember"] is False
    assert db["insert_apply"].await_args.kwargs["snapshot"] == {}
    for name in ("set_preferred_rules", "upsert_merchant_votes", "upsert_number_votes", "get_rule_rows",
                 "get_uncategorised_rows"):
        db[name].assert_not_awaited()
    assert result.groups[0]["earlier_waiting"] == 0


async def test_remember_on_sets_the_top_choice_votes_and_snapshots_first(db):
    a, b = row(GROCER, amount=100), row(GROCER, amount=300)
    old_rule = {"key_kind": "till", "key_value": "5555555", "category_id": uuid4(), "times_used": 4}
    db["get_rule_rows"].return_value = [old_rule]
    db["get_number_votes"].return_value = [
        {"number_kind": "till", "number": "5555555", "direction": "out", "category_name": "Household"}
    ]

    await run(db, [a, b], [ApplyGroup(transaction_ids=[a["id"], b["id"]], category_id=GROCERIES["id"],
                                      remember=True)])

    prefs = db["set_preferred_rules"].await_args.kwargs["preferences"]
    assert len(prefs) == 1
    assert (prefs[0]["key_kind"], prefs[0]["key_value"], prefs[0]["category_id"], prefs[0]["amount"]) == (
        "till", "5555555", GROCERIES["id"], 200.0)
    merchant = db["upsert_merchant_votes"].await_args.kwargs["votes"]
    number = db["upsert_number_votes"].await_args.kwargs["votes"]
    assert merchant == [{"merchant_key": "ACME SUPERMARKET", "direction": "out", "category_name": "Groceries"}]
    assert number == [{"number_kind": "till", "number": "5555555", "direction": "out", "category_name": "Groceries"}]

    snapshot = db["insert_apply"].await_args.kwargs["snapshot"]
    assert snapshot["rules"] == [old_rule]
    assert snapshot["rule_keys"][0]["key_value"] == "5555555"
    assert snapshot["number_votes"] == [
        {"number_kind": "till", "number": "5555555", "direction": "out", "category_name": "Household"}
    ]
    assert snapshot["merchant_votes"] == [
        {"merchant_key": "ACME SUPERMARKET", "direction": "out", "category_name": None}
    ]


async def test_remember_on_a_person_keeps_it_private(db):
    a = row(PERSON)
    await run(db, [a], [ApplyGroup(transaction_ids=[a["id"]], category_id=GROCERIES["id"], remember=True)])

    # The user's own memory only - no shared vote, nothing keyed by the name.
    assert db["set_preferred_rules"].await_args.kwargs["preferences"][0]["key_kind"] == "reference"
    assert db["upsert_merchant_votes"].await_args.kwargs["votes"] == []
    assert db["upsert_number_votes"].await_args.kwargs["votes"] == []
    snapshot = db["insert_apply"].await_args.kwargs["snapshot"]
    assert snapshot["merchant_votes"] == [] and snapshot["number_votes"] == []
    assert "JANE" not in str(snapshot)


async def test_remember_reports_rows_waiting_in_uncategorised(db):
    a = row(GROCER)
    db["get_uncategorised_rows"].return_value = [row(GROCER), row(GROCER), row(POWER)]

    result = await run(db, [a], [ApplyGroup(transaction_ids=[a["id"]], category_id=GROCERIES["id"],
                                            remember=True)])

    assert result.groups[0]["earlier_waiting"] == 2


# --- choosing the category --------------------------------------------------


async def test_use_suggestion_takes_the_groups_suggestion(db):
    a = row(GROCER)
    db["get_user_rules"].return_value = [{
        "key_kind": "till", "key_value": "5555555", "provider": None, "account_hash": None, "direction": "out",
        "category_id": GROCERIES["id"], "label": None, "times_used": 2, "avg_amount": None,
    }]
    result = await run(db, [a], [ApplyGroup(transaction_ids=[a["id"]], use_suggestion=True)])
    assert result.groups[0]["category_id"] == GROCERIES["id"]


VEG = {"id": uuid4(), "name": "Vegetables", "type": "expense"}


def till_rule(category, times_used, avg_amount):
    return {"key_kind": "till", "key_value": "5555555", "provider": None, "account_hash": None,
            "direction": "out", "category_id": category["id"], "label": None, "times_used": times_used,
            "avg_amount": avg_amount}


async def test_use_suggestion_files_each_line_under_its_own_suggestion(db):
    # ab-292: small lines suggest Vegetables, large ones Groceries.
    small, big1, big2 = row(GROCER, amount=350), row(GROCER, amount=6500), row(GROCER, amount=7000)
    db["get_user_categories"].return_value = [GROCERIES, VEG, SALARY]
    db["get_user_rules"].return_value = [till_rule(GROCERIES, 9, 6000), till_rule(VEG, 3, 400)]

    result = await run(db, [small, big1, big2], [
        ApplyGroup(transaction_ids=[small["id"], big1["id"], big2["id"]], use_suggestion=True)
    ])

    filed = {g["category_name"]: g["allocated"] for g in result.groups}
    assert filed == {"Groceries": [big1["id"], big2["id"]], "Vegetables": [small["id"]]}
    assert db["insert_apply"].await_count == 2
    assert len({g["group_key"] for g in result.groups}) == 1


async def test_use_suggestion_gives_lines_without_one_the_groups(db):
    a, b = row(GROCER, amount=100), row(GROCER, amount=120)
    db["get_user_rules"].return_value = [till_rule(GROCERIES, 2, None)]
    result = await run(db, [a, b], [ApplyGroup(transaction_ids=[a["id"], b["id"]], use_suggestion=True)])
    assert [g["category_name"] for g in result.groups] == ["Groceries"]
    assert result.groups[0]["allocated"] == [a["id"], b["id"]]


async def test_a_per_line_choice_learns_softly_and_casts_no_vote(db):
    a = row(GROCER, amount=350)
    db["get_user_categories"].return_value = [GROCERIES, VEG, SALARY]

    await run(db, [a], [ApplyGroup(transaction_ids=[a["id"]], category_id=VEG["id"], remember=True,
                                   per_line=True)])

    learned = db["learn_rules"].await_args.kwargs["preferences"]
    assert [(p["key_value"], p["category_id"], p["amount"]) for p in learned] == [("5555555", VEG["id"], 350.0)]
    db["set_preferred_rules"].assert_not_awaited()
    db["upsert_merchant_votes"].assert_not_awaited()
    db["upsert_number_votes"].assert_not_awaited()
    # Still undoable: the saved choices are snapshotted first.
    assert "rule_keys" in db["insert_apply"].await_args.kwargs["snapshot"]


async def test_use_suggestion_without_one_is_rejected(db):
    a = row(GROCER)
    with pytest.raises(AllocationRejected) as exc:
        await run(db, [a], [ApplyGroup(transaction_ids=[a["id"]], use_suggestion=True)])
    assert exc.value.status_code == 422


async def test_new_category_is_created_once_for_several_groups(db):
    a, b = row(GROCER), row(POWER)
    pets = {"id": uuid4(), "name": "Pets", "type": "expense"}
    db["create_categories"].return_value = [pets]
    result = await run(db, [a, b], [
        ApplyGroup(transaction_ids=[a["id"]], new_category_name="Pets"),
        ApplyGroup(transaction_ids=[b["id"]], new_category_name=" pets "),
    ])
    assert db["create_categories"].await_args.kwargs["categories"] == [("Pets", "expense")]
    assert {g["category_id"] for g in result.groups} == {pets["id"]}
    assert result.created_categories == [pets]


async def test_existing_category_name_is_reused(db):
    a = row(GROCER)
    result = await run(db, [a], [ApplyGroup(transaction_ids=[a["id"]], new_category_name="groceries")])
    db["create_categories"].assert_not_awaited()
    assert result.groups[0]["category_id"] == GROCERIES["id"]


# --- rejections -------------------------------------------------------------


@pytest.mark.parametrize("make,expected", [
    # another user's (or a missing) row isn't returned by the user-scoped lock
    (lambda a: ([], [ApplyGroup(transaction_ids=[a["id"]], category_id=GROCERIES["id"])]), 404),
    (lambda a: ([a], [ApplyGroup(transaction_ids=[a["id"]], category_id=uuid4())]), 404),
    (lambda a: ([a], [ApplyGroup(transaction_ids=[a["id"]], category_id=SALARY["id"])]), 422),
    (lambda a: ([a], [ApplyGroup(transaction_ids=[a["id"], a["id"]], category_id=GROCERIES["id"])]), 422),
])
async def test_rejections(db, make, expected):
    rows, groups = make(row(GROCER))
    with pytest.raises(AllocationRejected) as exc:
        await run(db, rows, groups)
    assert exc.value.status_code == expected
    db["insert_apply"].assert_not_awaited()


async def test_rows_of_different_payees_cant_share_a_group(db):
    a, b = row(GROCER), row(POWER)
    with pytest.raises(AllocationRejected) as exc:
        await run(db, [a, b], [ApplyGroup(transaction_ids=[a["id"], b["id"]], category_id=GROCERIES["id"])])
    assert exc.value.status_code == 422


@pytest.mark.parametrize("extra", [{"allocated": 100}, {"is_transfer": True}, {"handled_as": "charge"}])
async def test_rows_already_sorted_are_a_conflict(db, extra):
    a = row(GROCER, **extra)
    with pytest.raises(AllocationRejected) as exc:
        await run(db, [a], [ApplyGroup(transaction_ids=[a["id"]], category_id=GROCERIES["id"])])
    assert exc.value.status_code == 409


async def test_review_applies_only_take_that_imports_rows(db):
    a, b = row(GROCER), row(GROCER)
    db["get_import_rows"].return_value = [a]
    statement_import = {"id": uuid4(), "account_id": uuid4(), "period_start": None, "period_end": None}
    with pytest.raises(AllocationRejected) as exc:
        await run(db, [a, b], [ApplyGroup(transaction_ids=[a["id"], b["id"]], category_id=GROCERIES["id"])],
                  statement_import=statement_import)
    assert exc.value.status_code == 422

    db["get_import_rows"].return_value = [a, b]
    await run(db, [a, b], [ApplyGroup(transaction_ids=[a["id"], b["id"]], category_id=GROCERIES["id"])],
              statement_import=statement_import)
    assert db["insert_apply"].await_args.kwargs["import_id"] == statement_import["id"]


# --- undo -------------------------------------------------------------------


def applied(**extra):
    return {"id": uuid4(), "import_id": None, "category_id": GROCERIES["id"], "category_name": "Groceries",
            "group_key": "g:x", "remember": False, "snapshot": {}, "undone_at": None, **extra}


async def test_undo_reopens_the_rows(db):
    reopened = [uuid4(), uuid4()]
    a = applied()
    db["lock_apply"].return_value = a
    db["delete_apply_allocations"].return_value = reopened

    assert await undo_apply(object(), user_id=USER, apply_id=a["id"]) == reopened
    db["restore_snapshot"].assert_not_awaited()
    db["mark_apply_undone"].assert_awaited_once()


async def test_undo_of_a_remembered_apply_restores_the_snapshot(db):
    snapshot = {"rule_keys": [{"key_kind": "till"}], "rules": [], "merchant_votes": [], "number_votes": []}
    a = applied(remember=True, snapshot=snapshot)
    db["lock_apply"].return_value = a
    await undo_apply(object(), user_id=USER, apply_id=a["id"])
    assert db["restore_snapshot"].await_args.kwargs == {"user_id": USER, "snapshot": snapshot}


@pytest.mark.parametrize("found,code", [(None, 404), (applied(undone_at=datetime.now(timezone.utc)), 409)])
async def test_undo_rejections(db, found, code):
    db["lock_apply"].return_value = found
    with pytest.raises(AllocationRejected) as exc:
        await undo_apply(object(), user_id=USER, apply_id=uuid4())
    assert exc.value.status_code == code
    db["delete_apply_allocations"].assert_not_awaited()


# --- earlier rows -----------------------------------------------------------


async def test_earlier_rows_are_filed_under_the_same_category(db):
    first = row(GROCER)
    rows = await run(db, [first], [ApplyGroup(transaction_ids=[first["id"]], category_id=GROCERIES["id"],
                                              remember=True)])
    group_key = rows.groups[0]["group_key"]
    db["insert_apply"].reset_mock()
    db["set_preferred_rules"].reset_mock()

    waiting = [row(GROCER), row(GROCER)]
    other = row(POWER)
    db["lock_apply"].return_value = applied(group_key=group_key, remember=True)
    db["get_uncategorised_rows"].return_value = waiting + [other]
    db["lock_rows_for_apply"].return_value = waiting

    result = await apply_earlier(object(), user_id=USER, apply_id=uuid4(), secret=SECRET)

    assert db["lock_rows_for_apply"].await_args.kwargs["transaction_ids"] == [w["id"] for w in waiting]
    assert result.groups[0]["allocated"] == [w["id"] for w in waiting]
    assert result.groups[0]["category_id"] == GROCERIES["id"]
    assert db["insert_apply"].await_args.kwargs["remember"] is False
    db["set_preferred_rules"].assert_not_awaited()


async def test_earlier_with_nothing_waiting_does_nothing(db):
    db["lock_apply"].return_value = applied()
    result = await apply_earlier(object(), user_id=USER, apply_id=uuid4(), secret=SECRET)
    assert result.groups == []
    db["insert_apply"].assert_not_awaited()


@pytest.mark.parametrize("found,code", [(None, 404), (applied(undone_at=datetime.now(timezone.utc)), 409)])
async def test_earlier_rejections(db, found, code):
    db["lock_apply"].return_value = found
    with pytest.raises(AllocationRejected) as exc:
        await apply_earlier(object(), user_id=USER, apply_id=uuid4(), secret=SECRET)
    assert exc.value.status_code == code


# --- reopening lines of an apply (ab-292) -----------------------------------


async def test_reopen_takes_some_lines_out_of_an_apply(db):
    a = applied(remember=True, snapshot={"rule_keys": []})
    line = uuid4()
    db["lock_apply"].return_value = a
    db["delete_apply_line_allocations"].return_value = [line]
    db["count_apply_allocations"].return_value = 2

    assert await reopen_lines(object(), user_id=USER, apply_id=a["id"], transaction_ids=[line]) == [line]
    assert db["delete_apply_line_allocations"].await_args.kwargs == {"apply_id": a["id"], "transaction_ids": [line]}
    # The rest of the apply stands: nothing restored, not undone.
    db["restore_snapshot"].assert_not_awaited()
    db["mark_apply_undone"].assert_not_awaited()


async def test_reopening_the_last_lines_is_an_undo(db):
    snapshot = {"rule_keys": [{"key_kind": "till"}]}
    a = applied(remember=True, snapshot=snapshot)
    line = uuid4()
    db["lock_apply"].return_value = a
    db["delete_apply_line_allocations"].return_value = [line]
    db["count_apply_allocations"].return_value = 0

    await reopen_lines(object(), user_id=USER, apply_id=a["id"], transaction_ids=[line])

    assert db["restore_snapshot"].await_args.kwargs == {"user_id": USER, "snapshot": snapshot}
    db["mark_apply_undone"].assert_awaited_once()


async def test_reopen_rejects_lines_that_arent_in_the_apply(db):
    db["lock_apply"].return_value = applied()
    db["delete_apply_line_allocations"].return_value = []
    with pytest.raises(AllocationRejected) as exc:
        await reopen_lines(object(), user_id=USER, apply_id=uuid4(), transaction_ids=[uuid4()])
    assert exc.value.status_code == 422


@pytest.mark.parametrize("found,code", [(None, 404), (applied(undone_at=datetime.now(timezone.utc)), 409)])
async def test_reopen_rejections(db, found, code):
    db["lock_apply"].return_value = found
    with pytest.raises(AllocationRejected) as exc:
        await reopen_lines(object(), user_id=USER, apply_id=uuid4(), transaction_ids=[uuid4()])
    assert exc.value.status_code == code
    db["delete_apply_line_allocations"].assert_not_awaited()
