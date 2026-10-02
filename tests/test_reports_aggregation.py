"""Repository-level tests for app/repositories/reports.py's own Python
aggregation (build_report/build_csv_rows) - the real correctness-critical
logic behind ab-82/83/84/127, exercised directly against hand-built
fetch_report_rows()-shaped row lists rather than a mocked pool, so the
actual math is verified (not just that some mock returned the expected
number).
"""

from datetime import date, datetime, timezone
from decimal import Decimal
from uuid import uuid4

from app.repositories.reports import (
    ReportFilters,
    _resolve_transfer_source_label,
    build_account_summary,
    build_csv_rows,
    build_report,
    build_transaction_statement,
    filter_budget_plans_by_window,
    get_category_summary,
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
        "direction": None,
        "allocation_id": None,
        "alloc_amount": None,
        "transfer_reason_id": None,
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


def test_build_account_summary_sums_allocated_amount_by_direction_per_account():
    account_id = uuid4()
    rows = [
        _account_summary_row(
            account_id=account_id,
            transaction_id=uuid4(),
            direction="in",
            allocation_id=uuid4(),
            alloc_amount=Decimal("1000.00"),
        ),
        _account_summary_row(
            account_id=account_id,
            transaction_id=uuid4(),
            direction="out",
            allocation_id=uuid4(),
            alloc_amount=Decimal("300.00"),
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


def test_build_account_summary_excludes_a_transfer_shaped_allocation():
    account_id = uuid4()
    rows = [
        _account_summary_row(
            account_id=account_id,
            transaction_id=uuid4(),
            direction="in",
            allocation_id=uuid4(),
            alloc_amount=Decimal("1000.00"),
        ),
        # Transfer-shaped allocations aren't real income/expense - same
        # convention as build_report's total_in/total_out and
        # by_category.
        _account_summary_row(
            account_id=account_id,
            transaction_id=uuid4(),
            direction="out",
            allocation_id=uuid4(),
            alloc_amount=Decimal("500.00"),
            transfer_reason_id=uuid4(),
        ),
    ]

    summary = build_account_summary(rows)

    assert summary[0]["money_in"] == Decimal("1000.00")
    assert summary[0]["money_out"] == Decimal("0.00")


def test_build_account_summary_only_counts_the_reconciled_portion_of_a_transaction():
    """2026-10-02 feedback: money_in/money_out reflect what was actually
    RECONCILED (allocated), not a transaction's full amount - a
    1000.00 transaction with only 600.00 allocated to a category
    contributes 600.00, not 1000.00, and an allocation_id of None (no
    allocation at all) contributes nothing."""
    account_id = uuid4()
    txn_id = uuid4()
    rows = [
        _account_summary_row(
            account_id=account_id,
            transaction_id=txn_id,
            direction="in",
            allocation_id=uuid4(),
            alloc_amount=Decimal("600.00"),
        ),
        _account_summary_row(
            account_id=account_id,
            transaction_id=uuid4(),
            direction="in",
            allocation_id=None,
            alloc_amount=None,
        ),
    ]

    summary = build_account_summary(rows)

    assert summary[0]["money_in"] == Decimal("600.00")


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


# --- filter_budget_plans_by_window (ab-152) -------------------------------


def _plan(**overrides):
    base = {
        "id": uuid4(),
        "name": "This month",
        "starts_at": datetime(2026, 9, 1, tzinfo=timezone.utc),
        "ends_at": datetime(2026, 9, 30, tzinfo=timezone.utc),
        "total_cap": Decimal("5000.00"),
        "currency": "KES",
    }
    base.update(overrides)
    return base


def test_filter_budget_plans_by_window_keeps_a_plan_that_starts_before_from_but_ends_inside_the_range():
    # ab-152: true window-overlap, not just "starts within range" - this
    # plan started a full month before `from` but its window still
    # overlaps [from, to] because it ends inside it.
    plan = _plan(
        starts_at=datetime(2026, 8, 1, tzinfo=timezone.utc),
        ends_at=datetime(2026, 9, 10, tzinfo=timezone.utc),
    )

    kept = filter_budget_plans_by_window([plan], from_date=date(2026, 9, 1), to_date=date(2026, 9, 30))

    assert kept == [plan]


def test_filter_budget_plans_by_window_excludes_a_plan_entirely_before_the_range():
    plan = _plan(
        starts_at=datetime(2026, 7, 1, tzinfo=timezone.utc),
        ends_at=datetime(2026, 7, 31, tzinfo=timezone.utc),
    )

    kept = filter_budget_plans_by_window([plan], from_date=date(2026, 9, 1), to_date=date(2026, 9, 30))

    assert kept == []


def test_filter_budget_plans_by_window_excludes_a_plan_entirely_after_the_range():
    plan = _plan(
        starts_at=datetime(2026, 10, 5, tzinfo=timezone.utc),
        ends_at=datetime(2026, 10, 20, tzinfo=timezone.utc),
    )

    kept = filter_budget_plans_by_window([plan], from_date=date(2026, 9, 1), to_date=date(2026, 9, 30))

    assert kept == []


def test_filter_budget_plans_by_window_keeps_everything_when_both_bounds_are_omitted():
    plan = _plan()

    kept = filter_budget_plans_by_window([plan], from_date=None, to_date=None)

    assert kept == [plan]


def test_filter_budget_plans_by_window_only_bounds_the_side_that_is_given():
    # Only `from` given: a plan ending well before `from` is excluded,
    # but one starting far in the future (no `to` bound at all) is kept.
    old_plan = _plan(
        starts_at=datetime(2026, 1, 1, tzinfo=timezone.utc),
        ends_at=datetime(2026, 1, 31, tzinfo=timezone.utc),
    )
    future_plan = _plan(
        starts_at=datetime(2027, 1, 1, tzinfo=timezone.utc),
        ends_at=datetime(2027, 1, 31, tzinfo=timezone.utc),
    )

    kept = filter_budget_plans_by_window([old_plan, future_plan], from_date=date(2026, 9, 1), to_date=None)

    assert kept == [future_plan]


# --- get_category_summary (ab-152) ----------------------------------------


class _FakeCategorySummaryPool:
    """Minimal asyncpg.Pool stand-in for get_category_summary's own call
    to fetch_report_rows - returns a fixed set of fetch_report_rows()-
    shaped rows regardless of the query/args, since build_report's own
    aggregation correctness is already covered above; this only proves
    get_category_summary reuses that aggregation (rather than
    re-deriving a category breakdown from scratch) and filters it down
    to one category_type.
    """

    def __init__(self, rows):
        self._rows = rows

    async def fetch(self, query, *args):
        return self._rows


async def test_get_category_summary_reuses_by_category_aggregation_filtered_to_expense():
    rows = [
        _row(
            transaction_id=uuid4(),
            txn_amount=Decimal("500.00"),
            direction="out",
            allocation_id=uuid4(),
            category_id=GROCERIES_ID,
            category_name="Groceries",
            category_type="expense",
            alloc_amount=Decimal("500.00"),
        ),
        _row(
            transaction_id=uuid4(),
            txn_amount=Decimal("1000.00"),
            direction="in",
            allocation_id=uuid4(),
            category_id=SALARY_ID,
            category_name="Salary",
            category_type="income",
            alloc_amount=Decimal("1000.00"),
        ),
    ]
    pool = _FakeCategorySummaryPool(rows)
    filters = ReportFilters(user_id=uuid4(), currency="KES")

    result = await get_category_summary(pool, filters, category_type="expense")

    assert len(result) == 1
    assert result[0]["category_name"] == "Groceries"
    assert result[0]["category_type"] == "expense"
    assert result[0]["total"] == Decimal("500.00")


def _statement_row(**overrides):
    base = {
        "transaction_id": uuid4(),
        "txn_date": date(2026, 9, 1),
        "txn_amount": Decimal("0.00"),
        "direction": "in",
        "description": "Payment",
        "counterparty": None,
        "balance_after": None,
        "allocated_total": Decimal("0.00"),
    }
    base.update(overrides)
    return base


def test_build_transaction_statement_is_unknowable_for_an_all_manual_account():
    """2026-10-02 feedback: "balance b/f" only appears if we've uploaded a
    statement - an account with nothing but manual entries (no row ever
    had a real balance_after) has no anchor to compute from at all, so
    every balance - including opening_balance - is None."""
    rows = [
        _statement_row(txn_date=date(2026, 9, 1), txn_amount=Decimal("1000.00"), direction="in"),
        _statement_row(txn_date=date(2026, 9, 5), txn_amount=Decimal("200.00"), direction="out"),
    ]

    statement = build_transaction_statement(rows, from_date=None, to_date=None, status_filter=None)

    assert statement["opening_balance"] is None
    assert statement["closing_balance"] is None
    assert [row["balance"] for row in statement["rows"]] == [None, None]


def test_build_transaction_statement_computes_running_balance_from_an_anchor():
    """A statement-sourced row's balance_after is the anchor; every other
    row's balance is computed by replaying credit/debit from there -
    "based on the debit or credit then the balance will change"."""
    rows = [
        _statement_row(
            txn_date=date(2026, 9, 1), txn_amount=Decimal("1000.00"), direction="in", balance_after=Decimal("1000.00")
        ),
        _statement_row(txn_date=date(2026, 9, 5), txn_amount=Decimal("200.00"), direction="out"),
        _statement_row(txn_date=date(2026, 9, 10), txn_amount=Decimal("50.00"), direction="in"),
    ]

    statement = build_transaction_statement(rows, from_date=None, to_date=None, status_filter=None)

    assert statement["opening_balance"] == Decimal("0.00")
    assert [row["balance"] for row in statement["rows"]] == [
        Decimal("1000.00"),
        Decimal("800.00"),
        Decimal("850.00"),
    ]
    assert statement["closing_balance"] == Decimal("850.00")


def test_build_transaction_statement_back_calculates_balance_before_a_later_anchor():
    """The anchor doesn't have to be the first transaction - an earlier
    manual entry's balance is back-calculated by undoing the anchor's own
    delta and every delta between the manual entry and the anchor."""
    rows = [
        _statement_row(txn_date=date(2026, 9, 1), txn_amount=Decimal("100.00"), direction="in"),  # manual, no anchor
        _statement_row(
            txn_date=date(2026, 9, 5), txn_amount=Decimal("1000.00"), direction="in", balance_after=Decimal("1100.00")
        ),
    ]

    statement = build_transaction_statement(rows, from_date=None, to_date=None, status_filter=None)

    assert statement["opening_balance"] == Decimal("0.00")
    assert statement["rows"][0]["balance"] == Decimal("100.00")
    assert statement["rows"][1]["balance"] == Decimal("1100.00")


def test_build_transaction_statement_date_filter_keeps_true_opening_balance():
    """Filtering to a later date window still reflects every transaction
    that happened BEFORE it in opening_balance - a hidden transaction
    still really moved the balance, even though its own row isn't shown."""
    rows = [
        _statement_row(
            txn_date=date(2026, 9, 1), txn_amount=Decimal("1000.00"), direction="in", balance_after=Decimal("1000.00")
        ),
        _statement_row(txn_date=date(2026, 9, 10), txn_amount=Decimal("300.00"), direction="out"),
        _statement_row(txn_date=date(2026, 9, 20), txn_amount=Decimal("50.00"), direction="in"),
    ]

    statement = build_transaction_statement(rows, from_date=date(2026, 9, 15), to_date=None, status_filter=None)

    assert statement["opening_balance"] == Decimal("700.00")
    assert len(statement["rows"]) == 1
    assert statement["rows"][0]["balance"] == Decimal("750.00")
    assert statement["total_credit"] == Decimal("50.00")
    assert statement["total_debit"] == Decimal("0.00")


def test_build_transaction_statement_status_filter_hides_rows_but_not_their_balance_effect():
    """A row hidden by the status filter still shifts the balance shown
    on later displayed rows, and is excluded from the totals."""
    rows = [
        _statement_row(
            txn_date=date(2026, 9, 1),
            txn_amount=Decimal("1000.00"),
            direction="in",
            balance_after=Decimal("1000.00"),
            allocated_total=Decimal("1000.00"),  # fully allocated -> reconciled
        ),
        _statement_row(
            txn_date=date(2026, 9, 5),
            txn_amount=Decimal("100.00"),
            direction="out",
            allocated_total=Decimal("0.00"),  # unreconciled
        ),
        _statement_row(
            txn_date=date(2026, 9, 10),
            txn_amount=Decimal("40.00"),
            direction="in",
            allocated_total=Decimal("40.00"),  # reconciled
        ),
    ]

    statement = build_transaction_statement(rows, from_date=None, to_date=None, status_filter="reconciled")

    assert len(statement["rows"]) == 2
    assert statement["rows"][0]["balance"] == Decimal("1000.00")
    # The unreconciled -100.00 row isn't shown, but its effect carries
    # into the next displayed row's balance (900 - 100 + 40 = 940).
    assert statement["rows"][1]["balance"] == Decimal("940.00")
    assert statement["total_credit"] == Decimal("1040.00")
    assert statement["total_debit"] == Decimal("0.00")


def test_build_transaction_statement_status_derivation_matches_accounts_list_convention():
    """Fully allocated (>=) is reconciled; zero or partial is
    unreconciled - the same 2-state boundary accounts.py's own
    unreconciled_count uses."""
    rows = [
        _statement_row(txn_amount=Decimal("100.00"), allocated_total=Decimal("100.00")),
        _statement_row(txn_amount=Decimal("100.00"), allocated_total=Decimal("60.00")),
        _statement_row(txn_amount=Decimal("100.00"), allocated_total=Decimal("0.00")),
    ]

    statement = build_transaction_statement(rows, from_date=None, to_date=None, status_filter=None)

    assert [row["status"] for row in statement["rows"]] == ["reconciled", "unreconciled", "unreconciled"]


def test_build_transaction_statement_names_an_untitled_row_by_its_own_nature():
    """2026-10-02 feedback: a transaction with neither a description nor
    a counterparty is labeled "Money In"/"Money Out" by its own
    direction, not a bare "—" that told the viewer nothing."""
    rows = [
        _statement_row(description=None, counterparty=None, direction="in"),
        _statement_row(description=None, counterparty=None, direction="out"),
        _statement_row(description=None, counterparty="Naivas", direction="out"),
    ]

    statement = build_transaction_statement(rows, from_date=None, to_date=None, status_filter=None)

    assert [row["description"] for row in statement["rows"]] == ["Money In", "Money Out", "Naivas"]
