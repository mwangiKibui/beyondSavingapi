from pathlib import Path

import pytest

from app.parsers.mpesa import parse_mpesa_statement
from app.services.statement_files import decrypt_if_needed

FIXTURE = Path(__file__).parent / "fixtures" / "statements" / "mpesa" / "statement_sample.pdf"
FIXTURE_PASSWORD = "0000000"


@pytest.fixture(scope="module")
def decrypted_content() -> bytes:
    return decrypt_if_needed(
        content=FIXTURE.read_bytes(), filename="statement_sample.pdf", password=FIXTURE_PASSWORD
    )


@pytest.fixture(scope="module")
def transactions(decrypted_content):
    return parse_mpesa_statement(decrypted_content)


def test_parses_every_real_row_and_drops_orphan_continuation_rows(transactions):
    # 24 real rows on page 1 + 20 on page 2 = 44 - excludes the 4 phantom
    # "orphan" rows pdfplumber's table detection spuriously produces from
    # a wrapped counterparty name at the end of a cell (John/Doe, MUTISO,
    # KAMAU, NDUNG all duplicate text already in the row above).
    assert len(transactions) == 44


def test_returns_oldest_first(transactions):
    dates = [t["txn_date"] for t in transactions]
    assert dates == sorted(dates)
    assert transactions[0]["txn_date"].isoformat() == "2026-08-17"
    assert transactions[-1]["txn_date"].isoformat() == "2026-09-16"


def test_every_transaction_is_kes(transactions):
    assert all(t["currency"] == "KES" for t in transactions)


def test_fuliza_split_rows_are_kept_as_separate_transactions(transactions):
    # Receipt UIGJH6FJWZ covers a Fuliza-assisted merchant payment plus
    # its "OverDraft of Credit Party" credit - two distinct real-world
    # postings, not one - both must survive as separate rows.
    matches = [t for t in transactions if "PETER OMONDI" in (t["description"] or "")]
    assert len(matches) == 1
    payment = matches[0]
    assert payment["direction"] == "out"
    assert payment["amount"] == 30.0

    credit_matches = [
        t
        for t in transactions
        if t["description"] == "OverDraft of Credit Party" and t["balance_after"] == 30.0
    ]
    assert len(credit_matches) == 1
    assert credit_matches[0]["direction"] == "in"
    assert credit_matches[0]["amount"] == 30.0
    assert payment["dedupe_hash"] != credit_matches[0]["dedupe_hash"]


def test_handles_a_zero_amount_row(transactions):
    requests = [t for t in transactions if t["description"] == "M-Shwari Loan Request"]
    assert len(requests) == 2
    for request in requests:
        assert request["amount"] == 0.0
        assert request["direction"] == "in"


def test_counterparty_is_not_mapped(transactions):
    # Not in ab-39's field mapping for M-Pesa - the raw description already
    # carries whatever counterparty info the statement provides.
    assert all(t["counterparty"] is None for t in transactions)


def test_strips_stray_page_number_artifact_from_a_wrapped_description(transactions):
    matches = [t for t in transactions if "PETER OMONDI" in (t["description"] or "")]
    assert len(matches) == 1
    description = matches[0]["description"]
    # The bare page number "4" that bled into this cell as its own line
    # must not survive as a trailing word - real digits within the text
    # (the merchant number "7286859") are legitimate and must stay.
    assert description == "Merchant Payment Fuliza M-Pesa to 7286859 - PETER OMONDI"
    assert not description.endswith(" 4")


def test_does_not_duplicate_a_wrapped_counterparty_name(transactions):
    # KAMAU appears at the end of a wrapped Details cell, followed by a
    # phantom orphan row containing the same (sometimes truncated) name -
    # the real description must contain it exactly once, not twice.
    matches = [t for t in transactions if "254725***435" in (t["description"] or "")]
    assert len(matches) == 1
    assert matches[0]["description"].count("KAMAU") == 1


def test_dedupe_hash_is_stable_and_unique_per_real_transaction(transactions):
    hashes = [t["dedupe_hash"] for t in transactions]
    assert len(hashes) == len(set(hashes))
    assert all(len(h) == 64 for h in hashes)


def test_parsing_is_deterministic(decrypted_content):
    first_run = parse_mpesa_statement(decrypted_content)
    second_run = parse_mpesa_statement(decrypted_content)
    assert first_run == second_run


def test_resolves_the_leading_same_timestamp_cluster_into_a_reconciling_order(transactions):
    # The statement's first three rows (Disburse/Request/Repayment) all
    # share the exact timestamp 2026-08-17 12:42:43 - a naive stable sort
    # keeps the file's own top-to-bottom order, which does not replay
    # into a consistent balance chain (ab-42's finding). Only one of the
    # six permutations does - verified by hand against the real fixture.
    first_three = transactions[:3]
    assert [t["description"] for t in first_three] == [
        "M-Shwari Loan Disburse",
        "M-Shwari Loan Request",
        "OD Loan Repayment to 232323 - M-PESA Overdraw",
    ]
    assert [t["balance_after"] for t in first_three] == [2167.0, 2167.0, 1002.87]


def test_does_not_reorder_an_ambiguous_interior_tie(transactions):
    # Receipt UIGJH6FJWZ's Fuliza payment + OverDraft-of-Credit-Party
    # credit share a timestamp later in the file - both orderings
    # reconcile against neighboring rows equally well (M-Pesa's Balance
    # column isn't one continuous ledger around Fuliza entries), so this
    # must be left in the file's own original order rather than guessed.
    descriptions = [t["description"] for t in transactions[-2:]]
    assert descriptions == [
        "Merchant Payment Fuliza M-Pesa to 7286859 - PETER OMONDI",
        "OverDraft of Credit Party",
    ]
