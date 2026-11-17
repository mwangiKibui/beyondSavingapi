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
            "entry_type": "general",
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
            "saving": Decimal("0.00"),
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
            "saving": Decimal("0.00"),
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
        "category_names": [],
        "category_ids": [],
        "transfer_names": [],
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


def test_build_transaction_statement_reconciled_category_only_set_when_reconciled():
    """2026-10-02 feedback: "Reconciled Category" only has a value for a
    reconciled row - an unreconciled one is None (blank to the caller),
    even if it happens to carry category_names from a PARTIAL allocation."""
    rows = [
        _statement_row(
            txn_amount=Decimal("100.00"), allocated_total=Decimal("100.00"), category_names=["Groceries"]
        ),
        _statement_row(
            txn_amount=Decimal("100.00"), allocated_total=Decimal("40.00"), category_names=["Groceries"]
        ),
    ]

    statement = build_transaction_statement(rows, from_date=None, to_date=None, status_filter=None)

    assert statement["rows"][0]["reconciled_category"] == "Groceries"
    assert statement["rows"][1]["reconciled_category"] is None


def test_build_transaction_statement_reconciled_category_joins_categories_and_transfers():
    rows = [
        _statement_row(
            txn_amount=Decimal("100.00"),
            allocated_total=Decimal("100.00"),
            category_names=["Groceries", "Transport"],
        ),
        _statement_row(
            txn_amount=Decimal("100.00"), allocated_total=Decimal("100.00"), transfer_names=["Savings top-up"]
        ),
        _statement_row(txn_amount=Decimal("100.00"), allocated_total=Decimal("100.00")),
    ]

    statement = build_transaction_statement(rows, from_date=None, to_date=None, status_filter=None)

    assert statement["rows"][0]["reconciled_category"] == "Groceries, Transport"
    assert statement["rows"][1]["reconciled_category"] == "Savings top-up"
    # Reconciled with neither a category nor a transfer reason - ab-124's
    # "reconciled, no category" state - reads "No category", not blank.
    assert statement["rows"][2]["reconciled_category"] == "No category"


def test_build_transaction_statement_category_filter_hides_non_matching_rows_but_not_their_balance_effect():
    groceries_id = uuid4()
    rows = [
        _statement_row(
            txn_date=date(2026, 9, 1),
            txn_amount=Decimal("1000.00"),
            direction="in",
            balance_after=Decimal("1000.00"),
            allocated_total=Decimal("1000.00"),
            category_names=["Salary"],
            category_ids=[uuid4()],
        ),
        _statement_row(
            txn_date=date(2026, 9, 5),
            txn_amount=Decimal("100.00"),
            direction="out",
            allocated_total=Decimal("100.00"),
            category_names=["Groceries"],
            category_ids=[groceries_id],
        ),
        _statement_row(
            txn_date=date(2026, 9, 10),
            txn_amount=Decimal("40.00"),
            direction="out",
            allocated_total=Decimal("40.00"),
            category_names=["Groceries"],
            category_ids=[groceries_id],
        ),
    ]

    statement = build_transaction_statement(
        rows, from_date=None, to_date=None, status_filter=None, category_id=groceries_id
    )

    assert len(statement["rows"]) == 2
    assert [row["reconciled_category"] for row in statement["rows"]] == ["Groceries", "Groceries"]
    # The Salary row isn't shown (different category), but its +1000.00
    # still carries into the balance of the first displayed row
    # (1000 - 100 = 900).
    assert statement["rows"][0]["balance"] == Decimal("900.00")
    assert statement["total_debit"] == Decimal("140.00")


def test_build_transaction_statement_includes_each_row_s_transaction_id():
    txn_id = uuid4()
    rows = [_statement_row(transaction_id=txn_id, txn_date=date(2026, 9, 1), txn_amount=Decimal("100.00"))]

    statement = build_transaction_statement(rows, from_date=None, to_date=None, status_filter=None)

    assert statement["rows"][0]["id"] == txn_id


def test_build_transaction_statement_total_counts_every_displayed_row_regardless_of_paging():
    rows = [
        _statement_row(txn_date=date(2026, 9, i), txn_amount=Decimal("10.00"), direction="in")
        for i in range(1, 6)
    ]

    unpaged = build_transaction_statement(rows, from_date=None, to_date=None, status_filter=None)
    assert unpaged["total"] == 5
    assert len(unpaged["rows"]) == 5

    paged = build_transaction_statement(rows, from_date=None, to_date=None, status_filter=None, page=1, page_size=2)
    assert paged["total"] == 5
    assert len(paged["rows"]) == 2


def test_build_transaction_statement_pages_without_changing_totals_or_closing_balance():
    """page/page_size only slices which rows come back - total_credit/
    total_debit/closing_balance stay report-wide, computed from every
    displayed row, not just the current page."""
    rows = [
        _statement_row(
            txn_date=date(2026, 9, 1), txn_amount=Decimal("1000.00"), direction="in", balance_after=Decimal("1000.00")
        ),
        _statement_row(txn_date=date(2026, 9, 2), txn_amount=Decimal("100.00"), direction="out"),
        _statement_row(txn_date=date(2026, 9, 3), txn_amount=Decimal("50.00"), direction="out"),
    ]

    full = build_transaction_statement(rows, from_date=None, to_date=None, status_filter=None)
    page1 = build_transaction_statement(rows, from_date=None, to_date=None, status_filter=None, page=1, page_size=2)
    page2 = build_transaction_statement(rows, from_date=None, to_date=None, status_filter=None, page=2, page_size=2)

    assert [row["balance"] for row in page1["rows"]] == [Decimal("1000.00"), Decimal("900.00")]
    assert [row["balance"] for row in page2["rows"]] == [Decimal("850.00")]
    for statement in (page1, page2):
        assert statement["total"] == full["total"] == 3
        assert statement["closing_balance"] == full["closing_balance"] == Decimal("850.00")
        assert statement["total_debit"] == full["total_debit"] == Decimal("150.00")


# --- ab-281: loan rows handled at import --------------------------------------

def test_uncategorised_loan_rows_are_netted_out_like_transfers():
    payout = uuid4()
    repayment = uuid4()
    rows = [
        _row(transaction_id=payout, direction="in", txn_amount=Decimal("2000.00"), handled_as="loan",
             allocation_id=uuid4(), alloc_amount=Decimal("2000.00"), alloc_currency="KES"),
        _row(transaction_id=repayment, direction="out", txn_amount=Decimal("500.00"), handled_as="loan",
             allocation_id=uuid4(), category_id=GROCERIES_ID, category_name="Loan Repayment",
             category_type="expense", alloc_amount=Decimal("500.00"), alloc_currency="KES"),
    ]
    report = build_report(rows, currency="KES")
    assert report["total_in"] == Decimal("0.00")
    assert report["total_out"] == Decimal("500.00")


def test_account_summary_skips_uncategorised_loan_rows():
    account_id = uuid4()
    base = {"account_id": account_id, "account_nickname": "M-Pesa", "transaction_id": uuid4(),
            "allocation_id": uuid4(), "transfer_reason_id": None}
    rows = [
        {**base, "direction": "in", "alloc_amount": Decimal("2000.00"), "category_id": None, "handled_as": "loan"},
        {**base, "direction": "in", "alloc_amount": Decimal("300.00"), "category_id": SALARY_ID, "handled_as": None},
    ]
    assert build_account_summary(rows)[0]["money_in"] == Decimal("300.00")


# --- ab-259: the Saving bucket ------------------------------------------------


def test_saving_is_neither_income_nor_spending_and_counted_once():
    salary = _row(transaction_id=uuid4(), txn_amount=Decimal("50000.00"), direction="in",
                  allocation_id=uuid4(), category_id=uuid4(), category_name="Salary", category_type="income",
                  alloc_amount=Decimal("50000.00"))
    # Withdrawable side of a move into deposits: a transfer, netted out.
    move_out = _row(transaction_id=uuid4(), txn_amount=Decimal("3000.00"), direction="out", handled_as="transfer",
                    sub_ledger_role="spending", allocation_id=uuid4(), transfer_reason_id=uuid4(),
                    alloc_amount=Decimal("3000.00"))
    # Deposits side: the saving.
    move_in = _row(transaction_id=uuid4(), txn_amount=Decimal("3000.00"), direction="in", handled_as="saving",
                   sub_ledger_role="locked_saving", allocation_id=uuid4(), alloc_amount=Decimal("3000.00"))
    # A deposit withdrawal.
    exit_refund = _row(transaction_id=uuid4(), txn_amount=Decimal("500.00"), direction="out", handled_as="saving",
                       sub_ledger_role="locked_saving", allocation_id=uuid4(), alloc_amount=Decimal("500.00"))
    # Only M-Pesa uploaded: a Paybill to a money-market fund, split with groceries.
    mmf_txn = uuid4()
    mmf = _row(transaction_id=mmf_txn, txn_amount=Decimal("1200.00"), direction="out", allocation_id=uuid4(),
               category_id=uuid4(), category_name="Investments & money market", category_type="expense",
               category_entry_type="saving", alloc_amount=Decimal("1000.00"))
    food = _row(transaction_id=mmf_txn, txn_amount=Decimal("1200.00"), direction="out", allocation_id=uuid4(),
                category_id=uuid4(), category_name="Groceries", category_type="expense",
                category_entry_type="general", alloc_amount=Decimal("200.00"))

    report = build_report([salary, move_out, move_in, exit_refund, mmf, food], currency="KES")

    assert report["total_in"] == Decimal("50000.00")
    assert report["total_out"] == Decimal("200.00")
    assert report["total_saving"] == Decimal("3500.00")  # 3000 in - 500 out + 1000 MMF
    assert report["net"] == Decimal("46300.00")
    assert report["reconciled_no_category_total"] == Decimal("0.00")
    entry_types = {b["category_name"]: b["entry_type"] for b in report["by_category"]}
    assert entry_types["Investments & money market"] == "saving"


def test_withdrawable_side_alone_counts_as_saving():
    move_out = _row(transaction_id=uuid4(), txn_amount=Decimal("3000.00"), direction="out", handled_as="saving",
                    sub_ledger_role="spending", allocation_id=uuid4(), alloc_amount=Decimal("3000.00"))
    report = build_report([move_out], currency="KES")
    assert (report["total_out"], report["total_saving"]) == (Decimal("0.00"), Decimal("3000.00"))


def test_account_summary_shows_saving_apart():
    account_id = uuid4()
    rows = [
        _account_summary_row(account_id=account_id, transaction_id=uuid4(), direction="in", allocation_id=uuid4(),
                             alloc_amount=Decimal("3000.00"), handled_as="saving", sub_ledger_role="locked_saving"),
        _account_summary_row(account_id=account_id, transaction_id=uuid4(), direction="out", allocation_id=uuid4(),
                             alloc_amount=Decimal("700.00"), category_id=uuid4(), category_entry_type="saving"),
        _account_summary_row(account_id=account_id, transaction_id=uuid4(), direction="out", allocation_id=uuid4(),
                             alloc_amount=Decimal("200.00"), category_id=uuid4()),
    ]
    [item] = build_account_summary(rows)
    assert (item["money_in"], item["money_out"], item["saving"]) == (
        Decimal("0.00"), Decimal("200.00"), Decimal("3700.00")
    )
