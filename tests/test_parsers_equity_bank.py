from pathlib import Path

import pytest

from app.parsers.base import StatementParseError
from app.parsers.equity_bank import (
    _dedouble_word,
    _parse_pages,
    _parse_statement_text,
    parse_equity_bank_statement,
)
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


# ab-272: each row's description must hold only that row's own text.
# Equity prints a row's details around its amounts line (some above, some
# below), and the statement header sits above the table - neither may leak
# into a neighbouring row.
EXPECTED_DESCRIPTIONS = [
    "MPS 25470000009 9UA7JH2ZRWZ GRACE WANJIK DUOE 948FMNdRDgPh 0340100000001",
    "USSD/FRIENDLY CREDIT LIMITED/ U5C3A6F8A3460",
    "SMS CHARGE U5C3A6F8A3460",
    "25470000009 9UEDJH3XZN9 GRACE WANJIK DUOE 0340 GqCKj7dY4rIt 00GqCKj7dY4rIt",
    "25470000000 2UEDD4401OQ John Membe rDoe 03401 qMvPAahr0oxB 00qMvPAahr0oxB",
    "Interim statement printing charges Interim statmnt print chrgs",
]


def test_each_description_holds_only_its_own_row_text(transactions):
    assert [t["description"] for t in transactions] == EXPECTED_DESCRIPTIONS


def test_no_header_or_footer_text_in_any_description(transactions):
    # Header/footer labels as printed (case-sensitive: a real row may say
    # "Interim statement printing charges").
    banned = ["Account Number", "Statement Date", "Period", "Currency", "EXAMPLE.COM", "JANE", "Disclaimer", "Page 1/1"]
    for t in transactions:
        description = t["description"]
        assert not any(word in description for word in banned), description
        # No faux-bold doubled-letter header words (e.g. "TTrraannssaaccttiioonnss").
        assert all(_dedouble_word(w) == w for w in description.split() if w.isalpha()), description


def _synthetic_page(lines: list[str], start: float = 100.0) -> list[tuple[float, str]]:
    return [(start + 10 * i, text) for i, text in enumerate(lines)]


def test_later_pages_do_not_leak_their_header_or_previous_footer():
    heading = "TTrraannssaaccttiioonn DDeettaaiillss PPaayymmeenntt rreeffeerreennccee VVaalluuee DDaattee BBaallaannccee"
    page_one = _synthetic_page(
        [
            "Account Number 0000000000000",
            "Currency KES",
            "SAMPLE PERSON",
            heading,
            "SAMPLE SHOP ONE",
            "REF001 01/06/2026 100.00 100.00",
            "Disclaimer: sample footer text",
            "17/09/2026 Page 1/2",
        ]
    )
    page_two = _synthetic_page(
        [
            "Account Number 0000000000000",
            heading,
            "SAMPLE SHOP TWO",
            "REF002 02/06/2026 200.00 300.00",
            "WRAPPED TAIL TWO",
            "Total 300.00 0.00 300.00",
            "17/09/2026 Page 2/2",
        ]
    )
    transactions = _parse_pages([page_one, page_two])
    assert [t["description"] for t in transactions] == ["SAMPLE SHOP ONE", "SAMPLE SHOP TWO WRAPPED TAIL TWO"]
    assert [t["amount"] for t in transactions] == [100.0, 200.0]


def test_a_line_between_two_rows_goes_to_the_nearer_one():
    heading = "Transaction Details Payment reference Value Date Balance"
    page = [
        (100.0, heading),
        (120.0, "FIRST ROW TEXT"),
        (130.0, "REF001 01/06/2026 100.00 100.00"),
        (136.0, "FIRST ROW TAIL"),  # 6 below row 1, 24 above row 2
        (152.0, "SECOND ROW TEXT"),  # 22 below row 1, 8 above row 2
        (160.0, "REF002 02/06/2026 50.00 50.00"),
        (180.0, "Total 100.00 50.00 50.00"),
    ]
    transactions = _parse_pages([page])
    assert [t["description"] for t in transactions] == ["FIRST ROW TEXT FIRST ROW TAIL", "SECOND ROW TEXT"]
