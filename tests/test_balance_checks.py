"""ab-312: balance checks that report every failing row (the wizard's
preview needs them), next to the parsers' raise-on-first check."""

from datetime import date

import pytest

from app.parsers.base import (
    BalanceCheck,
    RunningBalanceMismatch,
    check_balances,
    running_balance_failures,
    totals_failures,
    validate_running_balance,
)


def txn(amount, direction, balance_after, description="row"):
    return {
        "txn_date": date(2026, 1, 1),
        "amount": amount,
        "currency": "KES",
        "direction": direction,
        "counterparty": None,
        "description": description,
        "balance_after": balance_after,
        "dedupe_hash": "x",
    }


ROWS = [txn(100, "in", 1100), txn(50, "out", 1050), txn(10, "out", 1040)]


def test_a_reconciling_chain_has_no_failures():
    assert running_balance_failures(ROWS, opening_balance=1000) == []


def test_every_failing_row_is_reported_re_anchoring_on_each_stated_balance():
    rows = [txn(100, "in", 1100), txn(50, "out", 999), txn(10, "out", 989), txn(5, "out", 900)]
    # Row 2 is off; row 3 re-anchors on row 2's stated 999 and passes; row 4 is off.
    assert running_balance_failures(rows, opening_balance=1000) == [1, 3]


def test_a_missing_balance_counts_as_a_failure():
    rows = [txn(100, "in", 1100), txn(50, "out", None)]
    assert running_balance_failures(rows, opening_balance=1000) == [1]


def test_validate_running_balance_keeps_its_message_and_first_failure():
    rows = [txn(100, "in", 1100), txn(50, "out", 999, "Groceries"), txn(5, "out", 900)]
    with pytest.raises(RunningBalanceMismatch) as exc:
        validate_running_balance(rows, opening_balance=1000)
    assert str(exc.value) == (
        "Running balance mismatch at transaction 2 (Groceries): expected 1050.00, statement shows 999.00"
    )


def test_totals_are_compared_to_a_cent():
    rows = [txn(100, "in", None), txn(50, "out", None), txn(25, "out", None)]
    assert totals_failures(rows, total_in=100, total_out=75.004) == []
    assert totals_failures(rows, total_in=90, total_out=70) == ["money in", "money out"]


def test_check_balances_uses_the_running_balance_when_rows_have_one():
    check = check_balances(ROWS, opening_balance=1000, totals=None)
    assert check == BalanceCheck(kind="running", passed=True, failing_rows=[], message=None)
    bad = check_balances([txn(100, "in", 1099)], opening_balance=1000, totals=None)
    assert bad.passed is False and bad.failing_rows == [0]
    assert "row 1" in bad.message


def test_check_balances_uses_totals_when_there_is_no_balance_column():
    rows = [txn(100, "in", None), txn(50, "out", None)]
    assert check_balances(rows, opening_balance=None, totals=(100, 50)).passed is True
    bad = check_balances(rows, opening_balance=None, totals=(100, 60))
    assert bad.kind == "totals" and bad.passed is False and "money out" in bad.message


def test_check_balances_fails_when_there_is_nothing_to_check_against():
    rows = [txn(100, "in", None)]
    check = check_balances(rows, opening_balance=None, totals=None)
    assert check.passed is False


def test_an_empty_statement_fails_the_check():
    assert check_balances([], opening_balance=1000, totals=None).passed is False
