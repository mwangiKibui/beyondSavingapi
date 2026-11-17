"""ab-281: handle_import / pair_transfers / undo / change category (repositories mocked)."""

from datetime import date
from unittest.mock import AsyncMock
from uuid import uuid4

import pytest

from app.services.import_handling import (
    HandlingRejected,
    change_handled_category,
    handle_import,
    undo_handling,
)

SVC = "app.services.import_handling"
SECRET = "test-secret"
USER = uuid4()
NCBA_ACC, MPESA_ACC = uuid4(), uuid4()
IMPORT = {"id": uuid4(), "account_id": NCBA_ACC, "provider": "NCBA Bank", "user_id": USER,
          "period_start": date(2026, 10, 1), "period_end": date(2026, 10, 31)}
CHARGES = {"id": uuid4(), "name": "Bank & mobile charges", "type": "expense"}
REPAYMENT = {"id": uuid4(), "name": "Loan Repayment", "type": "expense"}
REASON_ID = uuid4()


def txn(description, *, amount=500.0, direction="out", allocated=0, handled_as=None, dismissed=False, day=5):
    return {"id": uuid4(), "txn_date": date(2026, 10, day), "amount": amount, "currency": "KES",
            "direction": direction, "description": description, "is_transfer": False, "allocated": allocated,
            "handled_as": handled_as, "handling_dismissed": dismissed, "account_id": NCBA_ACC}


@pytest.fixture
def repo(monkeypatch):
    mocks = {
        "lock_import": AsyncMock(return_value={"handled_at": None}),
        "get_import_rows": AsyncMock(return_value=[]),
        "get_owned_providers": AsyncMock(return_value={"NCBA Bank", "M-Pesa"}),
        "create_categories": AsyncMock(return_value=[CHARGES, REPAYMENT]),
        "get_or_create_transfer_reason": AsyncMock(return_value=REASON_ID),
        "set_handled": AsyncMock(),
        "insert_handled_allocations": AsyncMock(),
        "delete_allocations": AsyncMock(),
        "get_waiting_transfer_legs": AsyncMock(return_value=[]),
        "get_pair_candidates": AsyncMock(return_value=[]),
        "link_transfer_pair": AsyncMock(),
        "mark_import_handled": AsyncMock(),
        "get_handled_transaction": AsyncMock(return_value=None),
        "dismiss_handling": AsyncMock(),
        "unlink_transfer": AsyncMock(),
        "get_category": AsyncMock(return_value=None),
    }
    for name, mock in mocks.items():
        monkeypatch.setattr(f"{SVC}.{name}", mock)
    return mocks


async def test_charges_loans_and_transfers_are_handled_with_reasons(repo):
    charge = txn("MOBILE PAYMENTS CHARGES 000000000000 FTC000000AAAA", amount=11.0)
    transfer = txn("TRF TO M-PESA 000000000000 FTC000000AAAA", amount=500.0)
    repayment = txn("AA LOAN REPAYMENT 0000000000 AA00000AAAA0 FT00000A0A0A", amount=3000.0)
    shop = txn("MERCHANT ATM TRANSACTION SOME SHOP NAIROBI", amount=900.0)
    repo["get_import_rows"].return_value = [charge, transfer, repayment, shop]

    result = await handle_import(object(), statement_import=IMPORT, secret=SECRET)

    assert result.counts == {"charge": 1, "transfer": 1, "loan": 1}
    handled = repo["set_handled"].call_args.kwargs
    assert set(handled["transaction_ids"]) == {charge["id"], transfer["id"], repayment["id"]}
    assert all(handled["reasons"])
    allocations = {a["transaction_id"]: a for a in repo["insert_handled_allocations"].call_args.kwargs["allocations"]}
    assert allocations[charge["id"]]["category_id"] == CHARGES["id"]
    assert allocations[repayment["id"]]["category_id"] == REPAYMENT["id"]
    # A waiting leg is netted out at once, sourced from the other side's name.
    assert allocations[transfer["id"]]["transfer_reason_id"] == REASON_ID
    assert allocations[transfer["id"]]["source_description"] == "M-Pesa"
    repo["mark_import_handled"].assert_awaited_once()


async def test_rows_already_categorised_dismissed_or_handled_are_left_alone(repo):
    repo["get_import_rows"].return_value = [
        txn("MOBILE PAYMENTS CHARGES 0 FTC0", amount=11.0, allocated=11.0),
        txn("MOBILE PAYMENTS CHARGES 0 FTC1", amount=11.0, dismissed=True),
        txn("MOBILE PAYMENTS CHARGES 0 FTC2", amount=11.0, handled_as="charge"),
    ]
    result = await handle_import(object(), statement_import=IMPORT, secret=SECRET)
    assert result.counts == {}
    repo["set_handled"].assert_not_awaited()


async def test_an_import_is_handled_only_once(repo):
    repo["lock_import"].return_value = {"handled_at": "2026-11-16T10:00:00Z"}
    assert await handle_import(object(), statement_import=IMPORT, secret=SECRET) is None
    repo["get_import_rows"].assert_not_awaited()
    repo["mark_import_handled"].assert_not_awaited()


async def test_zero_amount_loan_rows_are_handled_without_an_allocation(monkeypatch, repo):
    request = txn("M-Shwari Loan Request", amount=0.0, direction="in")
    repo["get_import_rows"].return_value = [request]
    await handle_import(object(), statement_import={**IMPORT, "provider": "M-Pesa"}, secret=SECRET)
    assert repo["set_handled"].call_args.kwargs["kinds"] == ["loan"]
    assert repo["insert_handled_allocations"].call_args.kwargs["allocations"] == []


def leg(provider, account_id, direction, description, *, amount=500.0, day=5, nickname="Acc"):
    return {"id": uuid4(), "account_id": account_id, "provider": provider, "nickname": nickname,
            "direction": direction, "amount": amount, "currency": "KES", "txn_date": date(2026, 10, day),
            "description": description, "handled_as": "transfer"}


async def test_a_waiting_leg_pairs_with_the_other_statement_and_both_point_at_each_other(repo):
    bank = leg("NCBA Bank", NCBA_ACC, "out", "TRF TO M-PESA 000000000000 FTC000000AAAA", nickname="NCBA")
    receipt = {**leg("M-Pesa", MPESA_ACC, "in", "Funds received from - 0000000000 SOMEONE", nickname="M-Pesa"),
               "handled_as": None}
    repo["get_waiting_transfer_legs"].return_value = [bank]
    repo["get_pair_candidates"].return_value = [bank, receipt]

    result = await handle_import(object(), statement_import=IMPORT, secret=SECRET)

    assert result.paired == 1
    link = repo["link_transfer_pair"].call_args.kwargs
    assert link["ids"] == [bank["id"], receipt["id"]]
    assert link["reasons"] == ["Moved to your M-Pesa account", "Moved from your NCBA account"]
    allocations = {a["transaction_id"]: a for a in repo["insert_handled_allocations"].call_args.kwargs["allocations"]}
    assert allocations[bank["id"]]["source_account_id"] == MPESA_ACC
    assert allocations[receipt["id"]]["source_account_id"] == NCBA_ACC


async def test_no_partner_leaves_the_leg_waiting(repo):
    repo["get_waiting_transfer_legs"].return_value = [
        leg("NCBA Bank", NCBA_ACC, "out", "TRF TO M-PESA 000000000000 FTC000000AAAA")
    ]
    result = await handle_import(object(), statement_import=IMPORT, secret=SECRET)
    assert result.paired == 0
    repo["link_transfer_pair"].assert_not_awaited()


# --- corrections -------------------------------------------------------------

def handled(description, *, handled_as, provider="NCBA Bank", paired=None, direction="out", amount=500.0):
    return {"id": uuid4(), "account_id": NCBA_ACC, "provider": provider, "direction": direction, "amount": amount,
            "currency": "KES", "description": description, "handled_as": handled_as, "handled_reason": "why",
            "transfer_group_id": uuid4() if paired else None, "paired_transaction_id": paired}


async def test_undo_a_charge_returns_it_to_review_for_good(repo):
    row = handled("MOBILE PAYMENTS CHARGES 0 FTC0", handled_as="charge")
    repo["get_handled_transaction"].return_value = row
    assert await undo_handling(object(), transaction_id=row["id"], user_id=USER, secret=SECRET) == [row["id"]]
    repo["delete_allocations"].assert_awaited_once_with(repo["delete_allocations"].call_args.args[0],
                                                       transaction_ids=[row["id"]])
    repo["dismiss_handling"].assert_awaited_once()


async def test_not_a_transfer_sends_a_claimed_partner_back_to_review(repo):
    other_id = uuid4()
    row = handled("TRF TO M-PESA 000000000000 FTC000000AAAA", handled_as="transfer", paired=other_id)
    partner = handled("Funds received from - 0000000000 SOMEONE", handled_as="transfer", provider="M-Pesa",
                      direction="in")
    partner["id"] = other_id
    repo["get_handled_transaction"].side_effect = [row, partner]

    back = await undo_handling(object(), transaction_id=row["id"], user_id=USER, secret=SECRET)

    assert back == [row["id"], other_id]
    repo["unlink_transfer"].assert_awaited_once()
    assert repo["set_handled"].call_args.kwargs == {"transaction_ids": [other_id], "kinds": [None], "reasons": [None]}


async def test_not_a_transfer_keeps_a_partner_that_is_a_transfer_by_itself_waiting(repo):
    other_id = uuid4()
    row = handled("Funds received from - 0000000000 SOMEONE", handled_as="transfer", provider="M-Pesa",
                  paired=other_id, direction="in")
    partner = handled("TRF TO M-PESA 000000000000 FTC000000AAAA", handled_as="transfer")
    partner["id"] = other_id
    repo["get_handled_transaction"].side_effect = [row, partner]

    back = await undo_handling(object(), transaction_id=row["id"], user_id=USER, secret=SECRET)

    assert back == [row["id"]]
    assert repo["set_handled"].call_args.kwargs["kinds"] == ["transfer"]
    waiting = repo["insert_handled_allocations"].call_args.kwargs["allocations"][0]
    assert waiting["transaction_id"] == other_id and waiting["source_description"] == "M-Pesa"


async def test_undo_rejects_rows_that_are_not_handled_or_not_the_users(repo):
    with pytest.raises(HandlingRejected) as missing:
        await undo_handling(object(), transaction_id=uuid4(), user_id=USER, secret=SECRET)
    assert missing.value.status_code == 404
    repo["get_handled_transaction"].return_value = handled("X", handled_as=None)
    with pytest.raises(HandlingRejected) as plain:
        await undo_handling(object(), transaction_id=uuid4(), user_id=USER, secret=SECRET)
    assert plain.value.status_code == 409


async def test_change_a_charges_category(repo):
    row = handled("MOBILE PAYMENTS CHARGES 0 FTC0", handled_as="charge", amount=11.0)
    fees = {"id": uuid4(), "name": "Fees", "type": "expense"}
    repo["get_handled_transaction"].return_value = row
    repo["get_category"].return_value = fees

    result = await change_handled_category(object(), transaction_id=row["id"], user_id=USER, category_id=fees["id"])

    assert result == {"transaction_id": row["id"], "category_id": fees["id"], "category_name": "Fees"}
    allocation = repo["insert_handled_allocations"].call_args.kwargs["allocations"][0]
    assert (allocation["category_id"], allocation["amount"]) == (fees["id"], 11.0)


@pytest.mark.parametrize(
    ("handled_as", "category", "status"),
    [
        ("transfer", {"id": uuid4(), "name": "Fees", "type": "expense"}, 409),
        ("charge", None, 404),
        ("charge", {"id": uuid4(), "name": "Salary", "type": "income"}, 422),
    ],
)
async def test_change_category_rejections(repo, handled_as, category, status):
    repo["get_handled_transaction"].return_value = handled("X", handled_as=handled_as)
    repo["get_category"].return_value = category
    with pytest.raises(HandlingRejected) as rejected:
        await change_handled_category(object(), transaction_id=uuid4(), user_id=USER, category_id=uuid4())
    assert rejected.value.status_code == status
