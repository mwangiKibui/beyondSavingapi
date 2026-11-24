"""ab-312: page-1 fingerprints - the layout's shape, never anyone's data."""

import re
from pathlib import Path

import pytest

from app.parsers.profiles.fingerprint import PageFingerprint, fingerprint_pdf
from app.services.statement_files import decrypt_if_needed
from tests.statement_pdfs import Layout, build, sample_rows

FIXTURES = Path(__file__).parent / "fixtures" / "statements"
REAL = {
    "mpesa": ("statement_sample.pdf", "0000000"),
    "ncba_bank": ("e_statement_sample.pdf", "0000000"),
    "equity_bank": ("account_statement_sample.pdf", "0000"),
}
# Names, emails and words from the fixtures' (synthetic) people.
PRIVATE = ["jane", "doe", "peter", "omondi", "grace", "wanjik", "sample@", "alex"]


@pytest.fixture(scope="module")
def real():
    out = {}
    for key, (name, password) in REAL.items():
        content = decrypt_if_needed(content=(FIXTURES / key / name).read_bytes(), filename=name, password=password)
        out[key] = fingerprint_pdf(content)
    return out


def test_mpesa_header_is_the_transaction_table_not_the_summary(real):
    fp = real["mpesa"]
    assert fp.header == [
        "receipt no",
        "completion time",
        "details",
        "transaction status",
        "paid in",
        "withdrawn",
        "balance",
    ]
    assert fp.cols == 7
    assert "statement period" in fp.labels
    assert fp.title == ["m-pesa statement"]


def test_ncba_two_line_header_is_merged(real):
    assert "value date" in real["ncba_bank"].header
    assert "account number" in real["ncba_bank"].labels


def test_equity_faux_bold_header_is_undoubled(real):
    header = real["equity_bank"].header
    assert header[0] == "transaction details"
    assert any("credit" in h for h in header)


def test_the_three_institutions_have_different_fingerprints(real):
    texts = {fp.to_text() for fp in real.values()}
    assert len(texts) == 3


def test_fingerprints_hold_no_digits_or_personal_details(real):
    for key, fp in real.items():
        text = fp.to_text().lower()
        # Only the column count and positions are numbers; no stored word has a digit.
        words = " ".join([*fp.title, *fp.labels, *fp.header])
        assert not re.search(r"\d", words), key
        for word in PRIVATE:
            assert word not in text, (key, word)


def test_the_same_file_always_gives_the_same_text(real):
    content = build(sample_rows())
    assert fingerprint_pdf(content).to_text() == fingerprint_pdf(content).to_text()
    assert PageFingerprint.from_text(real["mpesa"].to_text()) == real["mpesa"]


def test_another_month_and_another_person_give_the_same_fingerprint():
    a = build(sample_rows(6), Layout(labels=[("Account Name", "ALEX SAMPLE"), ("Statement Period", "October")]))
    b = build(
        sample_rows(14, start_balance=52.5, day0=3),
        Layout(labels=[("Account Name", "BRIAN OTHER"), ("Statement Period", "November")]),
        continuation={2: "extra reference text"},
    )
    assert fingerprint_pdf(a).to_text() == fingerprint_pdf(b).to_text()


def test_a_name_line_above_the_table_stays_out_of_the_header():
    fp = fingerprint_pdf(build(sample_rows(), Layout(name_line_above_table="ALEX SAMPLE MEMBER")))
    assert fp.header == ["date", "details", "money in", "money out", "balance"]
    assert "alex" not in fp.to_text().lower()


def test_values_after_a_colon_are_never_labels():
    fp = fingerprint_pdf(build(sample_rows()))
    assert "account name" in fp.labels
    assert all("alex" not in label for label in fp.labels)


def test_a_page_with_no_table_still_fingerprints_on_its_labels():
    fp = fingerprint_pdf(build([], Layout(end_marker=None)))
    assert fp.header == [] and fp.cols == 0
    assert "account name" in fp.labels


def test_not_a_pdf_gives_none():
    assert fingerprint_pdf(b"Date,Amount\n2026-10-01,5.00\n") is None
