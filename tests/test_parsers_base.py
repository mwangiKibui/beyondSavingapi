from datetime import date

import pytest

from app.parsers.base import ParsedTransaction, RunningBalanceMismatch, statement_period, validate_running_balance


def _txn(*, amount: float, direction: str, balance_after: float, description: str = "test") -> ParsedTransaction:
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


def test_passes_when_the_chain_reconciles():
    transactions = [
        _txn(amount=100.0, direction="in", balance_after=100.0),
        _txn(amount=40.0, direction="out", balance_after=60.0),
        _txn(amount=25.0, direction="in", balance_after=85.0),
    ]
    validate_running_balance(transactions, opening_balance=0.0)  # must not raise


def test_raises_on_the_first_row_that_does_not_reconcile():
    transactions = [
        _txn(amount=100.0, direction="in", balance_after=100.0),
        _txn(amount=40.0, direction="out", balance_after=100.0),  # should be 60.0
    ]
    with pytest.raises(RunningBalanceMismatch, match="transaction 2"):
        validate_running_balance(transactions, opening_balance=0.0)


def test_tolerates_subcent_floating_point_rounding():
    transactions = [_txn(amount=41340.99, direction="in", balance_after=41340.99)]
    validate_running_balance(transactions, opening_balance=0.0)  # must not raise


def test_statement_period_is_the_min_and_max_txn_date():
    transactions = [
        _txn(amount=1.0, direction="in", balance_after=1.0, description="a"),
        _txn(amount=1.0, direction="in", balance_after=1.0, description="b"),
    ]
    transactions[0]["txn_date"] = date(2026, 3, 1)
    transactions[1]["txn_date"] = date(2026, 1, 15)
    assert statement_period(transactions) == (date(2026, 1, 15), date(2026, 3, 1))
