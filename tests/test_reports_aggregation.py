"""Repository-level tests for app/repositories/reports.py's own Python
aggregation (build_report/build_csv_rows) - the real correctness-critical
logic behind ab-82/83/84/127, exercised directly against hand-built
fetch_report_rows()-shaped row lists rather than a mocked pool, so the
actual math is verified (not just that some mock returned the expected
number).
"""

from datetime import date
from decimal import Decimal
from uuid import uuid4

from app.repositories.reports import (
    _resolve_transfer_source_label,
    build_account_summary,
    build_csv_rows,
    build_report,
)

GROCERIES_ID = uuid4()
SALARY_ID = uuid4()
LOAN_REASON_ID = uuid4()


def _row(**overrides):
    base = {
        "transaction_id": None,
        "txn_date": date(2026, 10, 1),
        "txn_amount": Decimal("0.00"),
        "txn_currency": "KES",
        "direction": "out",
        "counterparty": "Someone",
        "description": "a transaction",
        "account_nickname": "KCB Salary",
        "allocation_id": None,
        "category_id": None,
        "category_name": None,
        "category_type": None,
        "transfer_reason_id": None,
        "alloc_amount": None,
        "alloc_currency": None,
    }
    base.update(overrides)
    return base


def test_an_unallocated_transaction_counts_fully_as_unreconciled():
    txn_id = uuid4()
    rows = [
        _row(
            transaction_id=txn_id,
            txn_amount=Decimal("1000.00"),
            direction="in",
            # LEFT JOIN allocations with no match: every allocation-side
            # column is NULL.
        )
    ]

    report = build_report(rows, currency="KES")

    assert report["total_in"] == Decimal("1000.00")
    assert report["total_out"] == Decimal("0.00")
    assert report["unreconciled_total"] == Decimal("1000.00")
    assert report["reconciled_no_category_total"] == Decimal("0.00")
    assert report["by_category"] == []


def test_a_fully_categorized_transaction_appears_in_by_category_not_unreconciled():
    txn_id = uuid4()
    rows = [
        _row(
            transaction_id=txn_id,
            txn_amount=Decimal("500.00"),
            direction="out",
            allocation_id=uuid4(),
            category_id=GROCERIES_ID,
            category_name="Groceries",
            category_type="expense",
            alloc_amount=Decimal("500.00"),
            alloc_currency="KES",
        )
    ]

    report = build_report(rows, currency="KES")

    assert report["total_out"] == Decimal("500.00")
    assert report["unreconciled_total"] == Decimal("0.00")
    assert report["by_category"] == [
        {
            "category_id": GROCERIES_ID,
            "category_name": "Groceries",
            "category_type": "expense",
            "total": Decimal("500.00"),
        }
    ]


def test_a_reconciled_no_category_allocation_lands_in_its_own_bucket_not_by_category():
    txn_id = uuid4()
    rows = [
        _row(
            transaction_id=txn_id,
            txn_amount=Decimal("300.00"),
            direction="out",
            allocation_id=uuid4(),
            category_id=None,
            transfer_reason_id=None,
            alloc_amount=Decimal("300.00"),
            alloc_currency="KES",
        )
    ]

    report = build_report(rows, currency="KES")

    assert report["total_out"] == Decimal("300.00")
    assert report["reconciled_no_category_total"] == Decimal("300.00")
    assert report["by_category"] == []
    assert report["unreconciled_total"] == Decimal("0.00")


def test_a_transfer_tagged_transaction_is_excluded_from_totals_but_its_remainder_is_still_unreconciled():
    txn_id = uuid4()
    rows = [
        _row(
            transaction_id=txn_id,
            txn_amount=Decimal("1000.00"),
            direction="out",
            allocation_id=uuid4(),
            category_id=None,
            transfer_reason_id=LOAN_REASON_ID,
            alloc_amount=Decimal("400.00"),
            alloc_currency="KES",
        )
    ]

    report = build_report(rows, currency="KES")

    # ab-79: excludes transfer-tagged transactions entirely from total_out,
    # even the portion the transfer allocation didn't cover.
    assert report["total_out"] == Decimal("0.00")
    assert report["total_in"] == Decimal("0.00")
    # A transfer-shaped allocation is neither a category nor a
    # reconciled-no-category bucket.
    assert report["by_category"] == []
    assert report["reconciled_no_category_total"] == Decimal("0.00")
    # But the 600.00 this transaction still hasn't allocated remains
    # unreconciled.
    assert report["unreconciled_total"] == Decimal("600.00")


def test_a_split_transaction_sums_into_the_same_category_bucket_and_computes_net():
    in_txn = uuid4()
    out_txn = uuid4()
    rows = [
        _row(
            transaction_id=in_txn,
            txn_amount=Decimal("200.00"),
            direction="in",
            allocation_id=uuid4(),
            category_id=SALARY_ID,
            category_name="Salary",
            category_type="income",
            alloc_amount=Decimal("100.00"),
            alloc_currency="KES",
        ),
        _row(
            transaction_id=in_txn,
            txn_amount=Decimal("200.00"),
            direction="in",
            allocation_id=uuid4(),
            category_id=SALARY_ID,
            category_name="Salary",
            category_type="income",
            alloc_amount=Decimal("100.00"),
            alloc_currency="KES",
        ),
        _row(
            transaction_id=out_txn,
            txn_amount=Decimal("50.00"),
            direction="out",
            allocation_id=uuid4(),
            category_id=GROCERIES_ID,
            category_name="Groceries",
            category_type="expense",
            alloc_amount=Decimal("50.00"),
            alloc_currency="KES",
        ),
    ]

    report = build_report(rows, currency="KES")

    assert report["total_in"] == Decimal("200.00")
    assert report["total_out"] == Decimal("50.00")
    assert report["net"] == Decimal("150.00")
    assert report["unreconciled_total"] == Decimal("0.00")
    totals_by_category = {item["category_name"]: item["total"] for item in report["by_category"]}
    assert totals_by_category == {"Salary": Decimal("200.00"), "Groceries": Decimal("50.00")}


def test_build_csv_rows_comma_joins_multiple_categories_and_dashes_an_unallocated_transaction():
    multi_cat_txn = uuid4()
    unallocated_txn = uuid4()
    rows = [
        _row(
            transaction_id=multi_cat_txn,
            txn_date=date(2026, 10, 2),
            txn_amount=Decimal("150.00"),
            direction="out",
            account_nickname="M-Pesa",
            counterparty="Naivas",
            description="Weekly shopping",
            allocation_id=uuid4(),
            category_id=GROCERIES_ID,
            category_name="Groceries",
            alloc_amount=Decimal("100.00"),
        ),
        _row(
            transaction_id=multi_cat_txn,
            txn_date=date(2026, 10, 2),
            txn_amount=Decimal("150.00"),
            direction="out",
            account_nickname="M-Pesa",
            counterparty="Naivas",
            description="Weekly shopping",
            allocation_id=uuid4(),
            category_id=SALARY_ID,
            category_name="Misc",
            alloc_amount=Decimal("50.00"),
        ),
        _row(
            transaction_id=unallocated_txn,
            txn_date=date(2026, 10, 1),
            txn_amount=Decimal("75.00"),
            direction="out",
            account_nickname="KCB Salary",
            counterparty=None,
            description=None,
        ),
    ]

    csv_rows = build_csv_rows(rows)

    multi_row = next(r for r in csv_rows if r["amount"] == Decimal("150.00"))
    assert multi_row["categories"] == "Groceries, Misc"

    unallocated_row = next(r for r in csv_rows if r["amount"] == Decimal("75.00"))
    assert unallocated_row["categories"] == "—"
    assert unallocated_row["counterparty"] == ""
    assert unallocated_row["description"] == ""


def _account_summary_row(**overrides):
    base = {
        "account_id": None,
        "account_nickname": "KCB Salary",
        "transaction_id": None,
        "txn_amount": None,
        "direction": None,
        "has_transfer_allocation": False,
    }
    base.update(overrides)
    return base


def test_build_account_summary_includes_an_account_with_zero_transactions():
    account_id = uuid4()
    rows = [_account_summary_row(account_id=account_id, account_nickname="Empty Account")]

    summary = build_account_summary(rows)

    assert summary == [
        {
            "account_id": account_id,
            "account_nickname": "Empty Account",
            "money_in": Decimal("0.00"),
            "money_out": Decimal("0.00"),
        }
    ]


def test_build_account_summary_sums_by_direction_per_account():
    account_id = uuid4()
    rows = [
        _account_summary_row(
            account_id=account_id,
            transaction_id=uuid4(),
            txn_amount=Decimal("1000.00"),
            direction="in",
        ),
        _account_summary_row(
            account_id=account_id,
            transaction_id=uuid4(),
            txn_amount=Decimal("300.00"),
            direction="out",
        ),
    ]

    summary = build_account_summary(rows)

    assert summary == [
        {
            "account_id": account_id,
            "account_nickname": "KCB Salary",
            "money_in": Decimal("1000.00"),
            "money_out": Decimal("300.00"),
        }
    ]


def test_build_account_summary_excludes_a_transaction_with_a_transfer_shaped_allocation():
    account_id = uuid4()
    rows = [
        _account_summary_row(
            account_id=account_id,
            transaction_id=uuid4(),
            txn_amount=Decimal("1000.00"),
            direction="in",
        ),
        # A transfer-shaped allocation on this one excludes it entirely
        # from money_out, same convention as build_report's total_out.
        _account_summary_row(
            account_id=account_id,
            transaction_id=uuid4(),
            txn_amount=Decimal("500.00"),
            direction="out",
            has_transfer_allocation=True,
        ),
    ]

    summary = build_account_summary(rows)

    assert summary[0]["money_in"] == Decimal("1000.00")
    assert summary[0]["money_out"] == Decimal("0.00")


def test_resolve_transfer_source_label_prefers_a_tracked_source_account():
    row = {
        "source_account_id": uuid4(),
        "source_account_nickname": "M-Pesa",
        "source_sub_ledger_id": uuid4(),
        "source_sub_ledger_name": "Emergency Fund",
        "source_sub_ledger_account_nickname": "KCB Savings",
        "source_description": "Should be ignored",
    }

    assert _resolve_transfer_source_label(row) == "M-Pesa"


def test_resolve_transfer_source_label_falls_back_to_sub_ledger_and_its_parent_account():
    row = {
        "source_account_id": None,
        "source_account_nickname": None,
        "source_sub_ledger_id": uuid4(),
        "source_sub_ledger_name": "Emergency Fund",
        "source_sub_ledger_account_nickname": "KCB Savings",
        "source_description": None,
    }

    assert _resolve_transfer_source_label(row) == "KCB Savings / Emergency Fund"


def test_resolve_transfer_source_label_falls_back_to_a_free_text_description():
    row = {
        "source_account_id": None,
        "source_account_nickname": None,
        "source_sub_ledger_id": None,
        "source_sub_ledger_name": None,
        "source_sub_ledger_account_nickname": None,
        "source_description": "Cash deposit from a friend",
    }

    assert _resolve_transfer_source_label(row) == "Cash deposit from a friend"


def test_resolve_transfer_source_label_dashes_when_nothing_is_set():
    row = {
        "source_account_id": None,
        "source_account_nickname": None,
        "source_sub_ledger_id": None,
        "source_sub_ledger_name": None,
        "source_sub_ledger_account_nickname": None,
        "source_description": None,
    }

    assert _resolve_transfer_source_label(row) == "—"
