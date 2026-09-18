from pathlib import Path

import pytest

from app.parsers.ncba_bank import parse_ncba_bank_statement
from app.services.statement_files import decrypt_if_needed

FIXTURE = Path(__file__).parent / "fixtures" / "statements" / "ncba_bank" / "e_statement_sample.pdf"
FIXTURE_PASSWORD = "0000000"


@pytest.fixture(scope="module")
def decrypted_content() -> bytes:
    return decrypt_if_needed(
        content=FIXTURE.read_bytes(), filename="e_statement_sample.pdf", password=FIXTURE_PASSWORD
    )


@pytest.fixture(scope="module")
def transactions(decrypted_content):
    return parse_ncba_bank_statement(decrypted_content)


def test_parses_every_real_transaction_across_all_pages(transactions):
    # 79 transactions spread across the statement's 5 pages - pdfplumber
    # merges every row on a page into one multi-line cell per column
    # (no inter-row grid lines), so this also proves the geometry-based
    # row reconstruction works across every page, not just the first.
    assert len(transactions) == 79


def test_returns_oldest_first(transactions):
    dates = [t["txn_date"] for t in transactions]
    assert dates == sorted(dates)
    assert transactions[0]["txn_date"].isoformat() == "2026-08-03"
    assert transactions[-1]["txn_date"].isoformat() == "2026-08-31"


def test_every_transaction_is_kes(transactions):
    assert all(t["currency"] == "KES" for t in transactions)


def test_balance_chain_matches_the_statement_own_closing_balance(transactions):
    assert transactions[-1]["balance_after"] == 99031.95


def test_totals_match_the_statement_own_summary_header(transactions):
    # Cross-checked directly against the statement's own "Payments In
    # 251,314.40" / "Payments Out 259,656.84" summary figures - proof
    # every transaction across all 5 pages was captured and signed
    # correctly, not just that some plausible-looking numbers came out.
    total_in = round(sum(t["amount"] for t in transactions if t["direction"] == "in"), 2)
    total_out = round(sum(t["amount"] for t in transactions if t["direction"] == "out"), 2)
    assert total_in == 251314.40
    assert total_out == 259656.84


def test_shared_reference_rows_get_different_dedupe_hashes(transactions):
    # "1009866765 AA261354HDCY FT26216QCX1Y" appears across three
    # consecutive rows (a commission fee, the clearing entry, and an
    # excise duty line) - not unique per row, per the runbook. Distinct
    # descriptions + amounts must still hash differently.
    matches = [t for t in transactions if "FT26216QCX1Y" in t["description"]]
    assert len(matches) == 3
    assert len({t["dedupe_hash"] for t in matches}) == 3


def test_counterparty_is_not_mapped(transactions):
    assert all(t["counterparty"] is None for t in transactions)


def test_dedupe_hash_is_stable_and_unique_per_transaction(transactions):
    hashes = [t["dedupe_hash"] for t in transactions]
    assert len(hashes) == len(set(hashes))
    assert all(len(h) == 64 for h in hashes)


def test_parsing_is_deterministic(decrypted_content):
    first_run = parse_ncba_bank_statement(decrypted_content)
    second_run = parse_ncba_bank_statement(decrypted_content)
    assert first_run == second_run
