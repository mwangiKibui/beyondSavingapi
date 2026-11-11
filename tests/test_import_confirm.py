"""ab-253 PR 2: confirm_import orchestration (repositories mocked)."""

from datetime import date, datetime, timezone
from unittest.mock import AsyncMock
from uuid import uuid4

import pytest

from app.services.allocations import AllocationRejected
from app.services.import_confirm import ConfirmItem, confirm_import

SVC = "app.services.import_confirm"
SECRET = "test-secret"
GROCERIES = {"id": uuid4(), "name": "Groceries", "type": "expense"}
SALARY = {"id": uuid4(), "name": "Salary", "type": "income"}
IMPORT = {"id": uuid4(), "account_id": uuid4(), "provider": "M-Pesa",
          "period_start": date(2026, 10, 1), "period_end": date(2026, 10, 31)}


def txn(description, *, amount=1000.0, direction="out", allocated=0):
    return {"id": uuid4(), "txn_date": date(2026, 10, 5), "amount": amount, "currency": "KES",
            "direction": direction, "description": description, "is_transfer": False,
            "allocated": allocated, "category_id": None}


@pytest.fixture
def repo(monkeypatch):
    def created(conn, *, user_id, categories):
        return [{"id": uuid4(), "name": n, "type": t} for n, t in categories]

    mocks = {
        "lock_transactions": AsyncMock(),
        "get_import_rows": AsyncMock(return_value=[]),
        "get_approved_directory": AsyncMock(return_value=[]),
        "get_blocked_numbers": AsyncMock(return_value=[]),
        "get_user_categories": AsyncMock(return_value=[GROCERIES, SALARY]),
        "create_categories": AsyncMock(side_effect=created),
        "insert_bulk_allocations": AsyncMock(),
        "upsert_rules": AsyncMock(),
        "set_rule_labels": AsyncMock(),
        "upsert_merchant_votes": AsyncMock(),
        "upsert_number_votes": AsyncMock(),
        "mark_import_reviewed": AsyncMock(return_value="2026-11-09T10:00:00Z"),
        "delete_progress": AsyncMock(return_value=True),
        "record_name_sightings": AsyncMock(),
        "get_learned_names": AsyncMock(return_value={}),
        "add_users_number_keys": AsyncMock(),
    }
    for name, mock in mocks.items():
        monkeypatch.setattr(f"{SVC}.{name}", mock)
    return mocks


async def confirm(items, user_id=None):
    return await confirm_import(object(), user_id=user_id or uuid4(), statement_import=IMPORT, items=items,
                                secret=SECRET)


async def test_existing_category_is_allocated_and_learned(repo):
    row = txn("Merchant Payment to 5555555 - NAIVAS LTD")
    repo["get_import_rows"].return_value = [row]

    result = await confirm([ConfirmItem(transaction_id=row["id"], category_id=GROCERIES["id"])])

    repo["lock_transactions"].assert_awaited_once()
    kwargs = repo["insert_bulk_allocations"].call_args.kwargs
    assert kwargs["transaction_ids"] == [row["id"]] and kwargs["category_ids"] == [GROCERIES["id"]]
    assert kwargs["amounts"] == [1000.0]
    rules = repo["upsert_rules"].call_args.kwargs["rules"]
    assert (rules[0]["key_kind"], rules[0]["key_value"], rules[0]["category_id"]) == ("till", "5555555", GROCERIES["id"])
    repo["mark_import_reviewed"].assert_awaited_once()
    # ab-265: finishing clears the saved progress in the same transaction.
    assert repo["delete_progress"].call_args.kwargs["import_id"] == IMPORT["id"]
    assert result.allocated == [row["id"]] and result.learned == 1 and result.created_categories == []


async def test_new_category_name_reuses_an_existing_one_ignoring_case(repo):
    row = txn("Merchant Payment to 5555555 - NAIVAS LTD")
    repo["get_import_rows"].return_value = [row]
    await confirm([ConfirmItem(transaction_id=row["id"], new_category_name="  groceries ")])
    repo["create_categories"].assert_not_awaited()
    assert repo["insert_bulk_allocations"].call_args.kwargs["category_ids"] == [GROCERIES["id"]]


async def test_new_category_is_created_once_with_the_rows_type(repo):
    a, b = txn("SHELL A LTD"), txn("SHELL B LTD")
    repo["get_import_rows"].return_value = [a, b]
    result = await confirm([ConfirmItem(transaction_id=a["id"], new_category_name="Fuel"),
                            ConfirmItem(transaction_id=b["id"], new_category_name="FUEL")])
    repo["create_categories"].assert_awaited_once()
    assert repo["create_categories"].call_args.kwargs["categories"] == [("Fuel", "expense")]
    ids = repo["insert_bulk_allocations"].call_args.kwargs["category_ids"]
    assert ids[0] == ids[1] == result.created_categories[0]["id"]


async def test_same_new_name_for_money_in_and_out_makes_two_categories(repo):
    out_row, in_row = txn("X LTD"), txn("Y LTD", direction="in")
    repo["get_import_rows"].return_value = [out_row, in_row]
    await confirm([ConfirmItem(transaction_id=out_row["id"], new_category_name="Side hustle"),
                   ConfirmItem(transaction_id=in_row["id"], new_category_name="Side hustle")])
    assert sorted(repo["create_categories"].call_args.kwargs["categories"]) == [
        ("Side hustle", "expense"), ("Side hustle", "income")]


async def test_row_outside_the_statement_is_rejected_before_any_write(repo):
    repo["get_import_rows"].return_value = [txn("A LTD")]
    with pytest.raises(AllocationRejected) as exc:
        await confirm([ConfirmItem(transaction_id=uuid4(), category_id=GROCERIES["id"])])
    assert exc.value.status_code == 422
    repo["create_categories"].assert_not_awaited()
    repo["insert_bulk_allocations"].assert_not_awaited()


async def test_category_type_must_match_direction(repo):
    row = txn("A LTD", direction="out")
    repo["get_import_rows"].return_value = [row]
    with pytest.raises(AllocationRejected) as exc:
        await confirm([ConfirmItem(transaction_id=row["id"], category_id=SALARY["id"])])
    assert exc.value.status_code == 422
    repo["insert_bulk_allocations"].assert_not_awaited()


async def test_unknown_category_is_404(repo):
    row = txn("A LTD")
    repo["get_import_rows"].return_value = [row]
    with pytest.raises(AllocationRejected) as exc:
        await confirm([ConfirmItem(transaction_id=row["id"], category_id=uuid4())])
    assert exc.value.status_code == 404


async def test_already_allocated_rows_are_skipped_and_teach_nothing(repo):
    done = txn("Merchant Payment to 5555555 - NAIVAS LTD", allocated=1000.0)
    repo["get_import_rows"].return_value = [done]
    result = await confirm([ConfirmItem(transaction_id=done["id"], category_id=GROCERIES["id"])])
    assert result.skipped == [done["id"]] and result.allocated == []
    repo["insert_bulk_allocations"].assert_not_awaited()
    repo["upsert_rules"].assert_awaited_once_with(repo["upsert_rules"].call_args.args[0],
                                                  user_id=repo["upsert_rules"].call_args.kwargs["user_id"], rules=[])


async def test_votes_and_labels_are_written(repo):
    row = txn("Merchant Payment to 5555555 - RUBIS ENERGY LTD")
    repo["get_import_rows"].return_value = [row]
    await confirm([ConfirmItem(transaction_id=row["id"], category_id=GROCERIES["id"], label="Fuel stop")])
    assert repo["upsert_merchant_votes"].call_args.kwargs["votes"][0]["merchant_key"] == "RUBIS ENERGY LTD"
    assert repo["upsert_number_votes"].call_args.kwargs["votes"][0]["number"] == "5555555"
    assert repo["set_rule_labels"].call_args.kwargs["labels"][0]["label"] == "Fuel stop"


async def test_entered_paybill_on_a_non_reference_row_is_rejected(repo):
    row = txn("Merchant Payment to 5555555 - NAIVAS LTD")
    repo["get_import_rows"].return_value = [row]
    with pytest.raises(AllocationRejected) as exc:
        await confirm([ConfirmItem(transaction_id=row["id"], category_id=GROCERIES["id"],
                                   user_entered_paybill="247247")])
    assert exc.value.status_code == 422


# --- ab-253 PR 4: number -> name learning ------------------------------------

NCBA_IMPORT = {**IMPORT, "provider": "NCBA Bank"}
SEEN = datetime(2026, 11, 1, tzinfo=timezone.utc)
NAIVAS_DIR = [{"merchant_id": uuid4(), "display_name": "Naivas", "category_name": "Groceries", "direction": "out",
               "key_kind": "name_pattern", "key_value": "NAIVAS", "requires_text": False}]


async def test_sightings_cover_every_row_in_scope_not_just_confirmed_ones(repo):
    a = txn("Merchant Payment to 5551234 - NAIVAS LTD")
    b = txn("Pay Bill to 888880 - KPLC PREPAID Acc. 12345678901")
    person = txn("Merchant Payment to 5559999 - JOHN KAMAU")
    repo["get_import_rows"].return_value = [a, b, person]
    user_id = uuid4()
    await confirm([ConfirmItem(transaction_id=a["id"], category_id=GROCERIES["id"])], user_id=user_id)
    kwargs = repo["record_name_sightings"].call_args.kwargs
    assert kwargs["user_id"] == user_id
    assert sorted(s["number"] for s in kwargs["sightings"]) == ["5551234", "888880"]


async def test_learned_name_votes_like_a_printed_one(repo):
    row = txn("IB Outgoing Mobile Payments BuyGoods FTX26232ABCDE 5551234")
    repo["get_import_rows"].return_value = [row]
    repo["get_learned_names"].return_value = {("till", "5551234", "out"): [
        {"name": "NAIVAS WESTLANDS LTD", "users": 1, "last_seen": SEEN}]}
    await confirm_import(object(), user_id=uuid4(), statement_import=NCBA_IMPORT,
                         items=[ConfirmItem(transaction_id=row["id"], category_id=GROCERIES["id"])], secret=SECRET)
    assert repo["upsert_merchant_votes"].call_args.kwargs["votes"][0]["merchant_key"] == "NAIVAS WESTLANDS LTD"
    assert repo["record_name_sightings"].call_args.kwargs["sightings"] == []  # borrowed names aren't re-sighted


async def test_three_users_seeing_a_listed_name_grow_the_list(repo):
    row = txn("Merchant Payment to 5551234 - NAIVAS WESTLANDS")
    repo["get_import_rows"].return_value = [row]
    repo["get_approved_directory"].return_value = NAIVAS_DIR
    repo["get_learned_names"].return_value = {("till", "5551234", "out"): [
        {"name": "NAIVAS WESTLANDS", "users": 3, "last_seen": SEEN}]}
    await confirm([ConfirmItem(transaction_id=row["id"], category_id=GROCERIES["id"])])
    assert repo["get_learned_names"].call_args.kwargs["numbers"] == [("till", "5551234", "out")]
    assert repo["add_users_number_keys"].call_args.kwargs["keys"] == [
        {"merchant_id": NAIVAS_DIR[0]["merchant_id"], "direction": "out", "key_kind": "till", "key_value": "5551234"}]


async def test_no_growth_below_three_users(repo):
    row = txn("Merchant Payment to 5551234 - NAIVAS WESTLANDS")
    repo["get_import_rows"].return_value = [row]
    repo["get_approved_directory"].return_value = NAIVAS_DIR
    repo["get_learned_names"].return_value = {("till", "5551234", "out"): [
        {"name": "NAIVAS WESTLANDS", "users": 2, "last_seen": SEEN}]}
    await confirm([ConfirmItem(transaction_id=row["id"], category_id=GROCERIES["id"])])
    assert repo["add_users_number_keys"].call_args.kwargs["keys"] == []
