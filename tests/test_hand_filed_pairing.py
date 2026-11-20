"""ab-285: a row filed by hand as a Funds transfer from/to one of the
user's accounts pairs with that account's row, in either upload order, so
the move is never counted twice; undo unpairs it."""

# ruff: noqa: F811 - test arguments named repo are the imported fixture
from datetime import date
from uuid import uuid4

from app.services.import_handling import pair_hand_filed_transfers, undo_handling, unpair_hand_filed
from app.services.row_handling import plan_hand_pairs
from tests.test_import_handling import REASON_ID, SECRET, USER, repo  # noqa: F401 - shared fixture

MPESA, SAVINGS, OTHER = uuid4(), uuid4(), uuid4()


def leg(*, amount=5000.0, day=10, source=SAVINGS, direction="in"):
    return {"id": uuid4(), "account_id": MPESA, "nickname": "M-Pesa", "direction": direction, "amount": amount,
            "currency": "KES", "txn_date": date(2026, 11, day), "source_account_id": source}


def cand(*, account=SAVINGS, amount=5000.0, day=10, direction="out", handled_as=None, role=None):
    return {"id": uuid4(), "account_id": account, "nickname": "Savings", "direction": direction, "amount": amount,
            "currency": "KES", "txn_date": date(2026, 11, day), "handled_as": handled_as, "sub_ledger_role": role}


# --- planning -------------------------------------------------------------------


def test_pairs_with_the_named_account_going_the_other_way():
    hand, c = leg(), cand()
    assert plan_hand_pairs([hand], [c]) == [(hand, c)]


def test_never_pairs_another_account_direction_amount_or_far_date():
    hand = leg()
    for c in (cand(account=OTHER), cand(direction="in"), cand(amount=4999.0), cand(day=13), cand(role="loan")):
        assert plan_hand_pairs([hand], [c]) == []


def test_closest_date_then_a_waiting_leg_wins_and_each_row_pairs_once():
    l1, l2 = leg(day=10), leg(day=10)
    far, near_open, near_leg = cand(day=12), cand(day=11), cand(day=11, handled_as="transfer")
    pairs = plan_hand_pairs([l1, l2], [far, near_open, near_leg])
    # The first leg takes the waiting leg at the same distance; the second
    # the open row; the farther row is left.
    assert [p[1]["id"] for p in pairs] == [near_leg["id"], near_open["id"]]


# --- pairing ----------------------------------------------------------------------


async def test_pairing_keeps_the_users_reason_and_hands_the_other_side(repo):
    hand, c = leg(), cand()
    repo["get_hand_filed_legs"].return_value = [hand]
    repo["get_hand_pair_candidates"].return_value = [c]

    assert await pair_hand_filed_transfers(object(), user_id=USER) == 1

    kwargs = repo["get_hand_pair_candidates"].await_args.kwargs
    assert kwargs["account_ids"] == [SAVINGS] and kwargs["user_id"] == USER
    link = repo["link_hand_pair"].await_args.kwargs
    assert (link["leg_id"], link["other_id"]) == (hand["id"], c["id"])
    assert link["other_reason"] == "Moved to your M-Pesa account"
    # Only the other side's allocations are replaced.
    assert repo["delete_allocations"].await_args.kwargs["transaction_ids"] == [c["id"]]
    [alloc] = repo["insert_handled_allocations"].await_args.kwargs["allocations"]
    assert (alloc["transaction_id"], alloc["transfer_reason_id"], alloc["source_account_id"]) == (
        c["id"], REASON_ID, MPESA)


async def test_nothing_to_pair(repo):
    assert await pair_hand_filed_transfers(object(), user_id=USER) == 0
    repo["get_hand_pair_candidates"].assert_not_awaited()


# --- undo -------------------------------------------------------------------------


async def test_undoing_the_hand_filed_row_sends_the_other_side_back_to_review(repo):
    hand, other = uuid4(), uuid4()
    repo["get_hand_paired"].return_value = [{"id": hand, "other_id": other}]
    repo["get_handled_transaction"].return_value = {
        "id": other, "handled_as": "transfer", "provider": "Equity Bank", "description": "CASH DEPOSIT",
        "direction": "out", "amount": 5000.0, "currency": "KES", "account_id": SAVINGS,
    }

    back = await unpair_hand_filed(object(), user_id=USER, transaction_ids=[hand], secret=SECRET)

    assert back == [other]
    unlinked = [c.kwargs["transaction_ids"] for c in repo["unlink_transfer"].await_args_list]
    assert unlinked == [[hand], [other]]
    assert repo["delete_allocations"].await_args.kwargs["transaction_ids"] == [other]


async def test_dismissing_the_handled_side_keeps_the_users_own_choice(repo):
    handled, hand = uuid4(), uuid4()
    rows = {
        handled: {"id": handled, "handled_as": "transfer", "paired_transaction_id": hand, "provider": "Equity Bank",
                  "description": "CASH", "direction": "out", "amount": 5000.0, "currency": "KES"},
        hand: {"id": hand, "handled_as": None, "provider": "M-Pesa", "description": "x", "direction": "in",
               "amount": 5000.0, "currency": "KES"},
    }
    repo["get_handled_transaction"].side_effect = lambda conn, transaction_id, user_id: rows[transaction_id]

    back = await undo_handling(object(), transaction_id=handled, user_id=USER, secret=SECRET)

    assert back == [handled]
    deleted = [c.kwargs["transaction_ids"] for c in repo["delete_allocations"].await_args_list]
    assert deleted == [[handled]]


# --- ab-293: a leg aimed at a deposits / shares sub-ledger -------------------------

DEPOSITS = uuid4()


def ledger_leg(**kw):
    """M-Pesa money out filed to the SACCO's deposits sub-ledger."""
    return {**leg(direction="out", **kw), "source_sub_ledger_id": DEPOSITS}


def ledger_cand(*, sub_ledger=DEPOSITS, handled_as="saving", role="locked_saving", **kw):
    return {**cand(direction="in", handled_as=handled_as, role=role, **kw), "sub_ledger_id": sub_ledger}


def test_a_ledger_leg_pairs_with_the_saving_row_on_that_sub_ledger():
    hand, c = ledger_leg(), ledger_cand()
    assert plan_hand_pairs([hand], [c]) == [(hand, c)]


def test_a_ledger_leg_never_pairs_another_sub_ledger_or_an_unhandled_row():
    hand = ledger_leg()
    for c in (ledger_cand(sub_ledger=uuid4()), ledger_cand(handled_as=None), ledger_cand(amount=1.0),
              ledger_cand(day=14)):
        assert plan_hand_pairs([hand], [c]) == []


def test_an_account_leg_still_never_pairs_a_saving_ledger_row():
    assert plan_hand_pairs([leg(direction="out")], [ledger_cand()]) == []


async def test_pairing_a_ledger_leg_keeps_the_saving_handling(repo):
    hand, c = ledger_leg(), ledger_cand()
    repo["get_hand_filed_legs"].return_value = [hand]
    repo["get_hand_pair_candidates"].return_value = [c]

    assert await pair_hand_filed_transfers(object(), user_id=USER) == 1

    ids = repo["set_transfer_group"].await_args.kwargs["ids"]
    assert ids == [hand["id"], c["id"]]
    repo["link_hand_pair"].assert_not_awaited()
    repo["delete_allocations"].assert_not_awaited()
    repo["insert_handled_allocations"].assert_not_awaited()


async def test_undoing_a_ledger_leg_leaves_the_deposit_saving(repo):
    hand, other = uuid4(), uuid4()
    repo["get_hand_paired"].return_value = [{"id": hand, "other_id": other}]
    repo["get_handled_transaction"].return_value = {
        "id": other, "handled_as": "saving", "provider": "Mentor Sacco", "description": "DEPOSIT",
        "direction": "in", "amount": 4000.0, "currency": "KES", "account_id": SAVINGS,
        "sub_ledger_role": "locked_saving",
    }

    back = await unpair_hand_filed(object(), user_id=USER, transaction_ids=[hand], secret=SECRET)

    assert back == []
    assert repo["set_handled"].await_args.kwargs["kinds"] == ["saving"]
