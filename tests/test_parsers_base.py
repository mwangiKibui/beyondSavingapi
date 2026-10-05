from datetime import date

import pytest

from app.parsers.base import (
    ParsedTransaction,
    RunningBalanceMismatch,
    derive_opening_balance,
    statement_period,
    validate_running_balance,
)


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


def test_derive_opening_balance_reverses_the_oldest_row_in_direction():
    transactions = [_txn(amount=200.0, direction="in", balance_after=1000.0)]
    transactions[0]["txn_date"] = date(2026, 9, 1)
    assert derive_opening_balance(transactions) == (800.0, date(2026, 9, 1))


def test_derive_opening_balance_reverses_an_outbound_oldest_row():
    transactions = [_txn(amount=200.0, direction="out", balance_after=800.0)]
    transactions[0]["txn_date"] = date(2026, 9, 1)
    assert derive_opening_balance(transactions) == (1000.0, date(2026, 9, 1))


def test_derive_opening_balance_only_looks_at_the_oldest_row():
    transactions = [
        _txn(amount=200.0, direction="in", balance_after=1000.0),
        _txn(amount=9999.0, direction="out", balance_after=1.0),  # later rows ignored
    ]
    transactions[0]["txn_date"] = date(2026, 9, 1)
    transactions[1]["txn_date"] = date(2026, 9, 2)
    assert derive_opening_balance(transactions) == (800.0, date(2026, 9, 1))


def test_derive_opening_balance_is_none_for_an_empty_list():
    assert derive_opening_balance([]) == (None, None)


def test_derive_opening_balance_is_none_when_the_oldest_row_has_no_balance_after():
    transactions = [_txn(amount=200.0, direction="in", balance_after=None)]
    assert derive_opening_balance(transactions) == (None, None)
