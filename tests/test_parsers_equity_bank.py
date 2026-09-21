from pathlib import Path

import pytest

from app.parsers.base import StatementParseError
from app.parsers.equity_bank import _parse_statement_text, parse_equity_bank_statement
from app.services.statement_files import decrypt_if_needed

FIXTURE = Path(__file__).parent / "fixtures" / "statements" / "equity_bank" / "account_statement_sample.pdf"
FIXTURE_PASSWORD = "0000"


@pytest.fixture(scope="module")
def decrypted_content() -> bytes:
    return decrypt_if_needed(
        content=FIXTURE.read_bytes(), filename="account_statement_sample.pdf", password=FIXTURE_PASSWORD
    )


@pytest.fixture(scope="module")
def transactions(decrypted_content):
    return parse_equity_bank_statement(decrypted_content)


def test_parses_every_real_transaction_and_excludes_the_total_row(transactions):
    # 6 real transactions in the fixture - the closing "Total" summary
    # row (rendered with doubled "bold" characters) must not appear as
    # a 7th transaction.
    assert len(transactions) == 6


def test_returns_oldest_first(transactions):
    dates = [t["txn_date"] for t in transactions]
    assert dates == sorted(dates)
    assert transactions[0]["txn_date"].isoformat() == "2026-01-06"
    assert transactions[-1]["txn_date"].isoformat() == "2026-05-13"


def test_every_transaction_is_kes(transactions):
    # Extracted from the statement's own "Currency KES" header field,
    # not hardcoded like M-Pesa.
    assert all(t["currency"] == "KES" for t in transactions)


def test_direction_and_amount_derived_from_balance_delta(transactions):
    # Verified by hand against the real fixture, including deriving the
    # opening balance (340.99) from the closing Total row, since there's
    # no explicit "Opening Balance" field for this provider.
    expected = [
        (41000.0, "in", 41340.99),
        (40500.0, "out", 840.99),
        (2.26, "out", 838.73),
        (400.0, "in", 1238.73),
        (1260.0, "in", 2498.73),
        (1260.0, "out", 1238.73),
    ]
    actual = [(t["amount"], t["direction"], t["balance_after"]) for t in transactions]
    assert actual == expected


def test_same_reference_rows_get_different_dedupe_hashes(transactions):
    # Payment reference 5492302 covers both a debit and its own SMS
    # charge - not unique per row, per the runbook. Combined with
    # description + amount, the two rows must still hash differently.
    same_reference = [t for t in transactions if t["balance_after"] in (840.99, 838.73)]
    assert len(same_reference) == 2
    assert same_reference[0]["dedupe_hash"] != same_reference[1]["dedupe_hash"]


def test_counterparty_is_not_mapped(transactions):
    assert all(t["counterparty"] is None for t in transactions)


def test_dedupe_hash_is_stable_and_unique_per_transaction(transactions):
    hashes = [t["dedupe_hash"] for t in transactions]
    assert len(hashes) == len(set(hashes))
    assert all(len(h) == 64 for h in hashes)


def test_parsing_is_deterministic(decrypted_content):
    first_run = parse_equity_bank_statement(decrypted_content)
    second_run = parse_equity_bank_statement(decrypted_content)
    assert first_run == second_run


def test_raises_when_the_closing_total_row_is_missing():
    # No opening balance can be derived without it (ab-42) - must fail
    # loudly rather than silently treating the first row as an opening
    # balance of 0.
    text_with_no_total_row = "Currency KES\n01/06/2026 REF001 01/06/2026 100.00 100.00\n"
    with pytest.raises(StatementParseError, match="Total"):
        _parse_statement_text(text_with_no_total_row)
