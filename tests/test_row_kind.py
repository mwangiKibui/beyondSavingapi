"""ab-287: one definition of what a row's money is."""

from decimal import Decimal
from itertools import product
from uuid import uuid4

import pytest

from app.repositories.dashboard import build_dashboard
from app.repositories.reports import build_csv_rows, build_account_summary, build_report
from app.services.row_kind import (
    BUCKET,
    COUNTED_KINDS,
    KINDS,
    allocation_kind,
    handled_saving,
    saved_amount,
    transaction_kind,
)

D = Decimal


def expected_kind(direction, handled_as, has_category, entry_type, has_transfer_reason, category_type):
    """The ticket's precedence table, written out independently."""
    if has_transfer_reason:
        return "transfer"
    if handled_as == "saving":
        return "saving"
    if handled_as == "loan" and not has_category:
        return "borrowed"
    if direction == "in" and category_type == "expense":
        return "refund"
    if direction == "out" and entry_type == "saving":
        return "saving"
    if direction == "out" and entry_type == "loan_repayment":
        return "loan_repayment"
    return "spending" if direction == "out" else "income"


# Every combination (an allocation with no category has no entry type
# or category type; only expense categories have a non-general entry type).
SIGNALS = [
    s for s in product(
        ("in", "out"),
        (None, "charge", "loan", "transfer", "saving"),
        (True, False),
        (None, "general", "saving", "loan_repayment"),
        (True, False),
        (None, "income", "expense"),
    )
    if (s[2] or (s[3] is None and s[5] is None)) and (not s[2] or s[5] is not None)
    and (s[5] != "income" or s[3] in (None, "general"))
]


@pytest.mark.parametrize(
    "direction, handled_as, has_category, entry_type, has_transfer_reason, category_type", SIGNALS)
def test_every_combination_of_signals(direction, handled_as, has_category, entry_type, has_transfer_reason,
                                      category_type):
    got = allocation_kind(direction=direction, handled_as=handled_as, has_category=has_category,
                          entry_type=entry_type, has_transfer_reason=has_transfer_reason, category_type=category_type)
    assert got == expected_kind(direction, handled_as, has_category, entry_type, has_transfer_reason, category_type)
    assert got in KINDS


def test_buckets():
    assert {k: BUCKET[k] for k in KINDS} == {
        "spending": "out", "loan_repayment": "out", "income": "in", "saving": "saving",
        "borrowed": None, "transfer": None, "refund": "out",
    }
    assert COUNTED_KINDS == {"spending", "loan_repayment", "income", "refund"}


def r(**kw):
    base = {"transfer_reason_id": None, "category_id": None, "handled_as": None}
    return {**base, **kw}


@pytest.mark.parametrize("rows, kind", [
    ([r(transfer_reason_id=uuid4()), r(category_id=uuid4())], "transfer"),     # any transfer split nets it out
    ([r(handled_as="loan")], "borrowed"),
    ([r(handled_as="loan", category_id=uuid4())], "counted"),                  # loan repaid under a category
    ([r(handled_as="saving")], "saving"),
    ([r(handled_as="charge", category_id=uuid4())], "counted"),
    ([r()], "counted"),
])
def test_transaction_kind(rows, kind):
    assert transaction_kind(rows) == kind


@pytest.mark.parametrize("direction, role, signed", [
    ("in", "locked_saving", D("100")), ("out", "locked_saving", D("-100")),
    ("in", "shares", D("100")), ("out", None, D("100")), ("in", None, D("-100")), ("in", "spending", D("-100")),
])
def test_handled_saving_sign(direction, role, signed):
    assert handled_saving(direction=direction, sub_ledger_role=role, amount=D("100")) == signed
    assert saved_amount(handled_as="saving", direction=direction, sub_ledger_role=role, amount=D("100")) == signed
    assert saved_amount(handled_as=None, direction=direction, sub_ledger_role=role, amount=D("100")) == D("100")


# --- golden totals: one fixture with every kind -------------------------------

ACC = uuid4()
GROC, SALARY, SAVE, LOAN = uuid4(), uuid4(), uuid4(), uuid4()
ENTRY = {GROC: "general", SALARY: "general", SAVE: "saving", LOAN: "loan_repayment"}
TYPE = {GROC: "expense", SALARY: "income", SAVE: "expense", LOAN: "expense"}


def txn(direction, amount, allocs, handled_as=None, role=None):
    """Joined (transaction, allocation) rows, as the report queries return them."""
    tid, out = uuid4(), []
    for cat, reason, amt in allocs or [(None, None, None)]:
        out.append({
            "transaction_id": tid, "txn_date": "2026-11-01", "txn_amount": D(amount), "txn_currency": "KES",
            "direction": direction, "counterparty": None, "description": None, "handled_as": handled_as,
            "sub_ledger_role": role, "account_id": ACC, "account_nickname": "M-Pesa",
            "allocation_id": uuid4() if amt is not None else None, "category_id": cat,
            "category_name": str(cat)[:4] if cat else None, "category_type": TYPE.get(cat),
            "category_entry_type": ENTRY.get(cat), "transfer_reason_id": reason,
            "alloc_amount": D(amt) if amt is not None else None, "alloc_currency": "KES",
        })
    return out


FIXTURE = [
    *txn("out", 1000, [(GROC, None, 1000)]),
    *txn("in", 50000, [(SALARY, None, 50000)]),
    *txn("out", 2000, [(SAVE, None, 2000)]),
    *txn("out", 3000, [(LOAN, None, 3000)]),
    *txn("out", 4000, [(GROC, None, 2500), (SAVE, None, 1500)]),
    *txn("in", 7000, [(None, uuid4(), 7000)]),
    *txn("out", 5000, [(None, uuid4(), 3000), (GROC, None, 2000)]),
    *txn("in", 10000, [(None, None, 10000)], handled_as="loan"),
    *txn("out", 1200, [(LOAN, None, 1200)], handled_as="loan"),
    *txn("out", 800, [(None, None, 800)]),
    *txn("out", 900, None),
    *txn("out", 1500, [(None, None, 1500)], handled_as="saving"),
    *txn("in", 3000, [(None, None, 3000)], handled_as="saving", role="locked_saving"),
]


def test_golden_report_totals():
    out = build_report(FIXTURE, currency="KES")
    # in: salary 50000 (transfer, borrowed and saving rows left out)
    # out: 1000 + (2000-2000) + 3000 + (4000-1500) + 1200 + 800 + 900 = 9400
    # saving: 2000 + 1500 + 1500 (handled, withdrawable side) + 3000 (deposit) = 8000
    assert (out["total_in"], out["total_out"], out["total_saving"]) == (D("50000"), D("9400"), D("8000"))
    assert out["net"] == D("50000") - D("9400") - D("8000")


def test_golden_account_summary_totals():
    [acc] = build_account_summary(FIXTURE)
    # by allocation: in 50000; out 1000+3000+2500+2000+1200+800 = 10500;
    # saving 2000+1500 + 1500 + 3000 = 8000
    assert (acc["money_in"], acc["money_out"], acc["saving"]) == (D("50000"), D("10500"), D("8000"))


def test_golden_dashboard_matches_the_report():
    """The dashboard totals whole transactions like the report."""
    by_txn: dict = {}
    for row in FIXTURE:
        by_txn.setdefault(row["transaction_id"], []).append(row)
    from app.services.row_kind import transaction_saving
    rows = [{
        "account_id": ACC, "account_nickname": "M-Pesa", "transaction_id": tid,
        "txn_amount": rows[0]["txn_amount"], "direction": rows[0]["direction"],
        "kind": transaction_kind(rows), "saving": transaction_saving(rows),
    } for tid, rows in by_txn.items()]
    dash = build_dashboard(rows, total_budget=D("0"), currency="KES")
    report = build_report(FIXTURE, currency="KES")
    assert (dash["total_in"], dash["total_out"], dash["total_saving"]) == (
        report["total_in"], report["total_out"], report["total_saving"])


def test_a_transfer_split_with_a_saving_part_is_netted_out_whole_everywhere():
    """A row split between a transfer and a saving category counts nowhere,
    in the report and the dashboard alike."""
    rows = txn("out", 5000, [(None, uuid4(), 3000), (SAVE, None, 2000)])
    report = build_report(rows, currency="KES")
    assert (report["total_out"], report["total_saving"]) == (D("0"), D("0"))
    from app.services.row_kind import transaction_saving
    dash = build_dashboard([{
        "account_id": ACC, "account_nickname": "M-Pesa", "transaction_id": rows[0]["transaction_id"],
        "txn_amount": D("5000"), "direction": "out", "kind": transaction_kind(rows),
        "saving": transaction_saving(rows),
    }], total_budget=D("0"), currency="KES")
    assert (dash["total_out"], dash["total_saving"]) == (D("0"), D("0"))



# --- ab-286: refunds ----------------------------------------------------------


def test_a_refund_lowers_spending_and_adds_no_income():
    """KES 1,000 groceries spent, then KES 400 refunded."""
    rows = [*txn("out", 1000, [(GROC, None, 1000)]), *txn("in", 400, [(GROC, None, 400)])]
    for r in rows:
        r["category_type"] = "expense"
    report = build_report(rows, currency="KES")
    assert (report["total_in"], report["total_out"]) == (D("0"), D("600"))
    [groc] = report["by_category"]
    assert groc["total"] == D("600")
    [acc] = build_account_summary(rows)
    assert (acc["money_in"], acc["money_out"]) == (D("0"), D("600"))


def test_refunds_beyond_spending_show_the_real_negative_number():
    rows = txn("in", 500, [(GROC, None, 500)])
    for r in rows:
        r["category_type"] = "expense"
    report = build_report(rows, currency="KES")
    assert (report["total_in"], report["total_out"], report["by_category"][0]["total"]) == (
        D("0"), D("-500"), D("-500"))


def test_part_refund_part_income_on_one_row():
    rows = txn("in", 1000, [(GROC, None, 300), (SALARY, None, 700)])
    report = build_report(rows, currency="KES")
    assert (report["total_in"], report["total_out"]) == (D("700"), D("-300"))


def test_dashboard_takes_refunds_off_money_out():
    dash = build_dashboard([{
        "account_id": ACC, "account_nickname": "M-Pesa", "transaction_id": uuid4(), "txn_amount": D("400"),
        "direction": "in", "kind": "counted", "saving": D("0"), "refund": D("400"),
    }], total_budget=D("0"), currency="KES")
    assert (dash["total_in"], dash["total_out"], dash["accounts"][0]["money_out"]) == (D("0"), D("-400"), D("-400"))



def test_the_export_labels_a_refund():
    rows = txn("in", 400, [(GROC, None, 400)])
    for r in rows:
        r.update(category_type="expense", category_name="Groceries", txn_date=None, account_nickname="M-Pesa",
                 txn_currency="KES", counterparty=None, description=None)
    [csv_row] = build_csv_rows(rows)
    assert csv_row["categories"] == "Refund · Groceries"


# --- ab-293: Funds transfers into / out of savings ------------------------------

from app.services.row_kind import transaction_saving, transfer_saving  # noqa: E402

MMF_ACC = uuid4()


def xfer(direction, amount, *, own="mobile_money", own_role=None, other=None, other_role=None, group=None,
         account=ACC, extra=None):
    """A Funds transfer row: own account type/sub-ledger role, the other side
    (source account type / source sub-ledger role) and its transfer group."""
    rows = txn(direction, amount, [(None, uuid4(), amount), *(extra or [])])
    for r in rows:
        r.update(account_type=own, sub_ledger_role=own_role, source_account_type=other,
                 source_sub_ledger_role=other_role, transfer_group_id=group, account_id=account)
    return rows


def dash_rows(rows):
    by_txn: dict = {}
    for row in rows:
        by_txn.setdefault(row["transaction_id"], []).append(row)
    return [{
        "account_id": rs[0]["account_id"], "account_nickname": "x", "transaction_id": tid,
        "txn_amount": rs[0]["txn_amount"], "direction": rs[0]["direction"],
        "kind": transaction_kind(rs), "saving": transaction_saving(rs),
    } for tid, rs in by_txn.items()]


def totals(rows):
    report = build_report(rows, currency="KES")
    dash = build_dashboard(dash_rows(rows), total_budget=D("0"), currency="KES")
    accounts = build_account_summary(rows)
    got = (report["total_in"], report["total_out"], report["total_saving"])
    assert got == (dash["total_in"], dash["total_out"], dash["total_saving"])
    assert sum((a["saving"] for a in accounts), D("0")) == report["total_saving"]
    return got


def ts(direction, *, own="mobile_money", own_role=None, other=None, other_role=None, paired=False):
    return transfer_saving(direction=direction, amount=D("100"), account_type=own, sub_ledger_role=own_role,
                           source_account_type=other, source_sub_ledger_role=other_role, paired=paired)


@pytest.mark.parametrize("kw, saved", [
    # spending side, other side a savings place, unpaired: counts
    ({"direction": "out", "other": "money_market"}, D("100")),
    ({"direction": "in", "other": "money_market"}, D("-100")),
    ({"direction": "out", "other": "sacco", "other_role": "locked_saving"}, D("100")),
    ({"direction": "out", "other": None, "other_role": "shares"}, D("100")),
    # paired: the savings side counts instead
    ({"direction": "out", "other": "money_market", "paired": True}, D("0")),
    # savings side: always counts, paired or not, tracked source or not
    ({"direction": "in", "own": "money_market", "other": "mobile_money"}, D("100")),
    ({"direction": "out", "own": "money_market", "other": "mobile_money", "paired": True}, D("-100")),
    ({"direction": "in", "own": "money_market"}, D("100")),
    ({"direction": "in", "own": "sacco", "own_role": "locked_saving", "other": "mobile_money"}, D("100")),
    ({"direction": "out", "own": "sacco", "own_role": "shares"}, D("-100")),
    # neutral: spending <-> spending, savings <-> savings, untracked spending side
    ({"direction": "out", "other": "bank"}, D("0")),
    ({"direction": "out"}, D("0")),
    ({"direction": "out", "own": "money_market", "other": "sacco", "other_role": "locked_saving"}, D("0")),
    # ab-259 withdrawable leg: other side is its own SACCO account
    ({"direction": "out", "own": "sacco", "own_role": "spending", "other": "sacco"}, D("0")),
])
def test_transfer_saving(kw, saved):
    direction = kw.pop("direction")
    assert ts(direction, **kw) == saved


def test_mpesa_to_mmf_is_saving_with_income_and_spending_unchanged():
    assert totals(xfer("out", 5000, other="money_market")) == (D("0"), D("0"), D("5000"))
    report = build_report(xfer("out", 5000, other="money_market"), currency="KES")
    assert report["net"] == D("-5000")


def test_mmf_to_mpesa_comes_off_saving():
    assert totals(xfer("in", 2000, other="money_market")) == (D("0"), D("0"), D("-2000"))


def test_both_legs_tracked_and_paired_count_once_on_the_savings_side():
    group = uuid4()
    rows = [*xfer("out", 5000, other="money_market", group=group),
            *xfer("in", 5000, own="money_market", other="mobile_money", group=group, account=MMF_ACC)]
    assert totals(rows) == (D("0"), D("0"), D("5000"))
    by_acc = {a["account_id"]: a["saving"] for a in build_account_summary(rows)}
    assert by_acc == {ACC: D("0"), MMF_ACC: D("5000")}


def test_withdrawal_paired_counts_once():
    group = uuid4()
    rows = [*xfer("out", 2000, own="money_market", other="mobile_money", group=group, account=MMF_ACC),
            *xfer("in", 2000, other="money_market", group=group)]
    assert totals(rows) == (D("0"), D("0"), D("-2000"))


def test_only_the_mmf_leg_uploaded_counts():
    assert totals(xfer("in", 5000, own="money_market", other="mobile_money")) == (D("0"), D("0"), D("5000"))


def test_spending_to_spending_stays_neutral():
    assert totals(xfer("out", 5000, other="bank")) == (D("0"), D("0"), D("0"))


def test_sacco_move_handled_as_saving_still_counts_once():
    """ab-259: deposit handled as saving + withdrawable leg a transfer to its
    own SACCO account."""
    group = uuid4()
    rows = [*xfer("out", 3000, own="sacco", own_role="spending", other="sacco", group=group),
            *txn("in", 3000, [(None, None, 3000)], handled_as="saving", role="locked_saving")]
    assert totals(rows)[2] == D("3000")


def test_hand_paired_leg_to_a_deposits_ledger_counts_once():
    """ab-293: an M-Pesa leg aimed at the deposits sub-ledger, paired with the
    credit handled as saving there."""
    group = uuid4()
    rows = [*xfer("out", 4000, other=None, other_role="locked_saving", group=group),
            *txn("in", 4000, [(None, None, 4000)], handled_as="saving", role="locked_saving")]
    for r in rows[1:]:
        r["transfer_group_id"] = group
    assert totals(rows)[2] == D("4000")


def test_only_the_transfer_part_of_a_mixed_row_is_saving():
    rows = xfer("out", 5000, other="money_market", extra=[(GROC, None, 2000)])
    rows[0]["alloc_amount"] = D("3000")
    assert totals(rows) == (D("0"), D("0"), D("3000"))
