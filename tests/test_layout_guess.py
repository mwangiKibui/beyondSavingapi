"""ab-315: the wizard's first guess and page snapshot."""

from pathlib import Path

import pytest

from app.parsers.profiles.guess import _role_for, layout_start
from app.parsers.profiles.reader import read_statement
from app.parsers.profiles.schema import validate_profile
from app.services.statement_files import decrypt_if_needed
from tests.statement_pdfs import Layout, build, sample_rows
from tests.statement_sheets import HEADER, PREAMBLE, csv_bytes, rows, xlsx_bytes

FIXTURES = Path(__file__).parent / "fixtures" / "statements"


def _passes(content: bytes, guess: dict) -> bool:
    result = read_statement(content, validate_profile(guess), default_currency="KES")
    return result.check.passed and len(result.transactions) > 1


@pytest.mark.parametrize(
    "heading, role",
    [
        ("Date", "date"),
        ("Completion Time", "date"),
        ("Narration", "description"),
        ("Transaction Type and Details", "description"),
        ("Paid In", "money_in"),
        ("Credit (Money In)", "money_in"),
        ("Withdrawn", "money_out"),
        ("Debit", "money_out"),
        ("Running Balance", "balance"),
        ("Receipt No.", "reference"),
        ("Amount", "signed_amount"),
        ("Branch", "ignore"),
    ],
)
def test_headings_suggest_roles(heading, role):
    assert _role_for(heading) == role


def test_a_simple_pdf_is_guessed_well_enough_to_pass():
    pdf = build(sample_rows(8))
    start = layout_start(pdf, "pdf")
    guess = start["guess"]
    assert [c["role"] for c in guess["table"]["columns"]] == ["date", "description", "money_in", "money_out", "balance"]
    assert guess["date_format"] == "%d/%m/%Y" and guess["table"]["column_bounds"] == "explicit"
    assert _passes(pdf, guess)


def test_newest_first_is_noticed_from_the_dates():
    pdf = build(list(reversed(sample_rows(6))))
    assert layout_start(pdf, "pdf")["guess"]["table"]["row_order"] == "newest_first"


def test_a_later_date_column_is_ignored():
    layout = Layout(
        headers=["Date", "Details", "Value Date", "Money In", "Money Out", "Balance"], xs=[40, 100, 260, 330, 410, 490]
    )
    rows_ = [[r[0], r[1], r[0], r[2], r[3], r[4]] for r in sample_rows(4)]
    roles = [c["role"] for c in layout_start(build(rows_, layout), "pdf")["guess"]["table"]["columns"]]
    assert roles == ["date", "description", "ignore", "money_in", "money_out", "balance"]


@pytest.mark.parametrize(
    "folder, name, password",
    [("equity_bank", "account_statement_sample.pdf", "0000"), ("ncba_bank", "e_statement_sample.pdf", "0000000")],
)
def test_known_bank_layouts_are_guessed_from_their_headings(folder, name, password):
    content = decrypt_if_needed(content=(FIXTURES / folder / name).read_bytes(), filename=name, password=password)
    assert _passes(content, layout_start(content, "pdf")["guess"])


def test_the_pdf_snapshot_is_page_one_lines_with_positions():
    start = layout_start(build(sample_rows(3)), "pdf")
    page = start["page"]
    assert page["width"] > 500 and page["height"] > 800 and start["page_count"] == 1
    texts = [" ".join(w["text"] for w in line["words"]) for line in page["lines"]]
    assert any(t.startswith("Date Details") for t in texts)
    word = page["lines"][0]["words"][0]
    assert set(word) == {"text", "x0", "x1"} and word["x1"] > word["x0"]


def test_a_page_with_no_table_has_no_guess_but_still_a_snapshot():
    start = layout_start(build([]), "pdf")
    assert start["guess"] is None and start["page"]["lines"]


@pytest.mark.parametrize("kind", ["csv", "xlsx"])
def test_spreadsheets_are_guessed_from_their_heading_row(kind):
    grid = [*PREAMBLE, HEADER, *rows(5)]
    content = csv_bytes(grid) if kind == "csv" else xlsx_bytes(grid)
    start = layout_start(content, kind)
    guess = start["guess"]
    assert guess["table"]["column_bounds"] == "cells"
    assert [c["header"] for c in guess["table"]["columns"]] == HEADER
    assert start["header_row"] == len(PREAMBLE) and start["rows"][len(PREAMBLE)] == HEADER
    assert _passes(content, guess)


def test_unfamiliar_headings_still_give_the_columns():
    layout = Layout(headers=["Txn Date", "Narrative", "Takings", "Outgoings", "Running Balance"])
    guess = layout_start(build(sample_rows(4), layout), "pdf")["guess"]
    assert [c["role"] for c in guess["table"]["columns"]] == ["date", "description", "ignore", "ignore", "balance"]
    assert all("x0" in c and "x1" in c for c in guess["table"]["columns"])


def test_received_and_sent_are_amounts():
    layout = Layout(headers=["Txn Date", "Narrative", "Received", "Sent", "Running Balance"])
    pdf = build(sample_rows(6), layout)
    guess = layout_start(pdf, "pdf")["guess"]
    assert [c["role"] for c in guess["table"]["columns"]] == ["date", "description", "money_in", "money_out", "balance"]
    assert _passes(pdf, guess)
