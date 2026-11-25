"""ab-325: statements whose fees come off the balance without a row of their
own (Airtel Money), 12-hour dates, column examples and typed headings.
All statements here are synthetic."""

import pytest

from app.parsers.profiles.guess import layout_start
from app.parsers.profiles.reader import UNLISTED_CHARGE, read_statement
from app.parsers.profiles.schema import InvalidStatementProfile, validate_profile
from app.services.merchant_keys import extract_key
from app.services.statement_layouts import check_layout
from tests.statement_pdfs import Layout, build

HEADERS = ["Transaction ID", "Transaction Date", "Description", "Status", "Credit", "Debit", "Balance"]
XS = [30, 105, 200, 330, 380, 440, 500]

# (when, description, money in, money out, hidden fee)
MOVES = [
    ("02-09-26 12:17 PM", "Sent Money to Shop A", 0, 100, 0),
    ("02-09-26 02:58 PM", "Received from Sample B", 3000, 0, 0),
    ("03-09-26 08:16 PM", "Sent Money to Store C", 0, 800, 13),
    ("05-09-26 09:01 AM", "Bundle purchase", 0, 50, 0),
    ("07-09-26 07:19 PM", "Sent Money to Shop D", 0, 150, 7),
    ("09-09-26 11:45 AM", "Sent Money to Store E", 0, 105, 5),
]
OPENING = 271.0


def _rows(moves=MOVES, opening=OPENING):
    rows, balance = [], opening
    for i, (when, desc, money_in, money_out, fee) in enumerate(moves):
        balance += money_in - money_out - fee
        rows.append(
            [
                f"TX{i:04d}AB",
                when,
                desc,
                "Success",
                f"{money_in:,.2f}" if money_in else "",
                f"{money_out:,.2f}" if money_out else "",
                f"{balance:,.2f}",
            ]
        )
    return rows, balance


def _statement(moves=MOVES, *, total_debit=None, opening=OPENING) -> bytes:
    rows, closing = _rows(moves, opening)
    total_in = sum(m[2] for m in moves)
    total_out = sum(m[3] + m[4] for m in moves) if total_debit is None else total_debit
    layout = Layout(
        title="Wallet Statement",
        labels=[
            ("Opening Balance", f"KES {opening:,.2f}"),
            ("Closing Balance", f"KES {closing:,.2f}"),
            ("Total Credit", f"KES {total_in:,.2f}"),
            ("Total Debit", f"KES {total_out:,.2f}"),
        ],
        headers=HEADERS,
        xs=XS,
    )
    return build(rows, layout)


def _profile(**overrides) -> dict:
    profile = {
        "file_type": "pdf",
        "date_format": "%d-%m-%y %I:%M %p",
        "table": {
            "columns": [
                {"role": "reference", "header": "Transaction ID"},
                {"role": "date", "header": "Transaction Date"},
                {"role": "description", "header": "Description"},
                {"role": "status", "header": "Status"},
                {"role": "money_in", "header": "Credit"},
                {"role": "money_out", "header": "Debit"},
                {"role": "balance", "header": "Balance"},
            ],
            "column_bounds": "header_gaps",
        },
        "opening_balance": {"source": "labelled_line", "label": "Opening Balance"},
        "totals": {"source": "labelled_lines", "label": "Total Credit", "out_label": "Total Debit"},
        "unlisted_charges": True,
    }
    profile.update(overrides)
    return profile


def _read(content: bytes, profile: dict):
    return read_statement(content, validate_profile(profile), default_currency="KES")


# --- the reader ---------------------------------------------------------------


def test_hidden_fees_become_charge_rows_after_their_transaction():
    result = _read(_statement(), _profile())
    assert result.check.passed, result.check.message
    txns = result.transactions
    assert len(txns) == len(MOVES) + 3
    charges = [t for t in txns if t["description"] == UNLISTED_CHARGE]
    assert [t["amount"] for t in charges] == [13.0, 7.0, 5.0]
    assert all(t["direction"] == "out" for t in charges)
    i = txns.index(charges[0])
    parent = txns[i - 1]
    assert parent["description"] == "Sent Money to Store C"
    assert charges[0]["txn_date"] == parent["txn_date"]
    # The transaction's own balance is what it would have been without the fee.
    assert parent["balance_after"] == pytest.approx(charges[0]["balance_after"] + 13)
    assert len({t["dedupe_hash"] for t in txns}) == len(txns)


def test_charges_must_be_proved_by_the_statement_totals():
    # Total Debit without the fees: the gaps aren't fees the statement owns up to.
    content = _statement(total_debit=sum(m[3] for m in MOVES))
    result = _read(content, _profile())
    assert not result.check.passed
    assert "total" in (result.check.message or "").lower()


def test_a_gap_on_money_in_is_not_a_charge():
    moves = [*MOVES[:2], ("04-09-26 10:00 AM", "Received from Sample F", 500, 0, 9)]
    result = _read(_statement(moves), _profile())
    assert not result.check.passed


def test_a_balance_higher_than_expected_is_not_a_charge():
    moves = [*MOVES[:2], ("04-09-26 10:00 AM", "Sent Money to Shop G", 0, 200, -9)]
    result = _read(_statement(moves), _profile())
    assert not result.check.passed


def test_without_the_option_hidden_fees_still_fail():
    profile = _profile()
    profile.pop("unlisted_charges")
    result = _read(_statement(), profile)
    assert not result.check.passed
    assert result.check.failing_rows


def test_unlisted_charges_need_statement_totals_and_a_balance():
    with pytest.raises(InvalidStatementProfile):
        validate_profile(_profile(totals={"source": "none"}))


def test_the_charge_row_is_handled_as_a_charge_for_any_provider():
    key = extract_key(provider="Airtel Money", description=UNLISTED_CHARGE, direction="out", secret="s")
    assert key.is_charge


# --- the first guess -------------------------------------------------------------


def test_twelve_hour_dates_and_hidden_fees_are_guessed():
    guess = layout_start(_statement(), "pdf")["guess"]
    assert guess["date_format"] == "%d-%m-%y %I:%M %p"
    assert guess["unlisted_charges"] is True
    assert guess["opening_balance"] == {"source": "labelled_line", "label": "Opening Balance"}
    assert guess["totals"]["label"] == "Total Credit" and guess["totals"]["out_label"] == "Total Debit"
    assert _read(_statement(), guess).check.passed


def test_no_charges_option_when_the_balances_already_add_up():
    moves = [m[:4] + (0,) for m in MOVES]
    guess = layout_start(_statement(moves), "pdf")["guess"]
    assert not guess.get("unlisted_charges")
    assert _read(_statement(moves), guess).check.passed


def test_each_guessed_column_has_an_example_from_the_statement():
    start = layout_start(_statement(), "pdf")
    examples = start["examples"]
    assert len(examples) == len(start["guess"]["table"]["columns"])
    assert examples[1] == "02-09-26 12:17 PM"
    assert examples[2] == "Sent Money to Shop A"


# --- typed headings ----------------------------------------------------------------


def test_typed_headings_read_the_statement():
    check = check_layout(_statement(), validate_profile(_profile()), currency="KES")
    assert check.passed and check.usable


def test_a_typed_heading_not_on_the_statement_is_named():
    profile = _profile()
    profile["table"]["columns"][2] = {"role": "description", "header": "Narrative"}
    check = check_layout(_statement(), validate_profile(profile), currency="KES")
    assert not check.passed
    assert check.message == "We couldn't find “Narrative” on your statement. Check the spelling."


def test_a_typed_heading_not_in_a_spreadsheet_is_named():
    from tests.statement_sheets import HEADER, PREAMBLE, csv_bytes, rows

    content = csv_bytes([*PREAMBLE, HEADER, *rows()])
    guess = layout_start(content, "csv")["guess"]
    guess["table"]["columns"][0] = {**guess["table"]["columns"][0], "header": "Value Dt"}
    check = check_layout(content, validate_profile(guess), currency="KES")
    assert check.message == "We couldn't find “Value Dt” on your statement. Check the spelling."


def test_centred_headings_over_left_aligned_values_keep_whole_descriptions():
    # Airtel's shape: headings centred, values starting well left of them.
    layout = Layout(
        headers=["Transaction Date", "Description", "Credit", "Debit", "Balance"],
        xs=[100, 200, 360, 430, 500],
        header_xs=[105, 262, 370, 440, 505],
    )
    rows = [
        ["02-09-26 12:17 PM", "Sent Money to Shop A", "", "100.00", "900.00"],
        ["03-09-26 08:16 PM", "Sent Money to Store B", "", "50.00", "850.00"],
        ["04-09-26 09:01 AM", "Received from Sample C", "200.00", "", "1,050.00"],
    ]
    content = build(rows, layout)
    start = layout_start(content, "pdf")
    assert start["examples"][:2] == ["02-09-26 12:17 PM", "Sent Money to Shop A"]
    result = _read(content, start["guess"])
    assert [t["description"] for t in result.transactions][0] == "Sent Money to Shop A"
