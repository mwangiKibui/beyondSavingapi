"""ab-312: the shared reader - any profile over a PDF, parser-shaped rows."""

import hashlib
from datetime import date
from pathlib import Path

import pytest

from app.parsers.base import RunningBalanceMismatch, StatementParseError
from app.parsers.profiles.reader import read_statement, read_transactions
from app.parsers.profiles.schema import validate_profile
from app.services.statement_files import decrypt_if_needed
from tests.statement_pdfs import Layout, build, sample_rows

KEYS = {"txn_date", "amount", "currency", "direction", "counterparty", "description", "balance_after", "dedupe_hash"}


def profile(**changes):
    raw = {
        "date_format": "%d/%m/%Y",
        "table": {
            "columns": [
                {"role": "date", "header": "Date"},
                {"role": "description", "header": "Details"},
                {"role": "money_in", "header": "Money In"},
                {"role": "money_out", "header": "Money Out"},
                {"role": "balance", "header": "Balance"},
            ],
            "column_bounds": "header_gaps",
            "end_markers": ["End of statement"],
        },
    }
    for key, value in changes.items():
        if key == "table":
            raw["table"] = {**raw["table"], **value}
        else:
            raw[key] = value
    return validate_profile(raw)


def test_reads_a_simple_statement_into_parser_shaped_rows():
    rows = read_transactions(build(sample_rows(6)), profile(), default_currency="KES")
    assert len(rows) == 6
    assert set(rows[0]) == KEYS
    assert rows[0]["txn_date"] == date(2026, 10, 1)
    assert rows[0]["direction"] == "in" and rows[0]["amount"] == 100.0 and rows[0]["balance_after"] == 1100.0
    assert rows[1]["direction"] == "out" and rows[1]["amount"] == 110.0 and rows[1]["balance_after"] == 990.0
    assert rows[0]["description"] == "Payment from Shop A"
    assert rows[0]["currency"] == "KES" and rows[0]["counterparty"] is None


def test_the_profile_currency_wins_over_the_accounts():
    rows = read_transactions(build(sample_rows(2)), profile(currency="USD"), default_currency="KES")
    assert {r["currency"] for r in rows} == {"USD"}


def test_reads_every_page_with_a_repeated_header_and_skips_page_numbers():
    rows = read_transactions(build(sample_rows(60), Layout(rows_per_page=20)), profile(), default_currency="KES")
    assert len(rows) == 60
    assert not any("Page" in (r["description"] or "") for r in rows)


def test_a_continuation_page_without_a_header_still_reads():
    pdf = build(sample_rows(40), Layout(rows_per_page=20, repeat_header=False))
    p = profile(table={"header_repeats_each_page": False})
    assert len(read_transactions(pdf, p, default_currency="KES")) == 40


def test_a_wrapped_description_joins_its_row():
    pdf = build(sample_rows(4), continuation={1: "ref ABC branch Westlands"})
    rows = read_transactions(pdf, profile(), default_currency="KES")
    assert len(rows) == 4
    assert rows[1]["description"] == "Paid to Store B ref ABC branch Westlands"


def test_ruled_columns():
    pdf = build(sample_rows(5), Layout(ruled=True))
    rows = read_transactions(pdf, profile(table={"column_bounds": "ruled"}), default_currency="KES")
    assert len(rows) == 5 and rows[4]["balance_after"] == 1120.0


def test_explicit_columns():
    width = 595.2755905511812
    xs = [40, 110, 330, 410, 490, 565]
    columns = [
        {"role": role, "x0": xs[i] / width, "x1": xs[i + 1] / width}
        for i, role in enumerate(["date", "description", "money_in", "money_out", "balance"])
    ]
    p = profile(table={"columns": columns, "column_bounds": "explicit", "header_repeats_each_page": False})
    assert len(read_transactions(build(sample_rows(5)), p, default_currency="KES")) == 5


def test_an_opening_balance_row_and_skip_rows():
    layout = Layout(pre_rows=[["", "Balance brought forward", "", "", "1,000.00"]], post_rows=[["", "Subtotal", "", "", ""]])
    p = profile(
        opening_balance={"source": "table_row", "label": "Balance brought forward"},
        table={"skip_rows": [{"match": "starts_with", "text": "Subtotal"}]},
    )
    result = read_statement(build(sample_rows(4), layout), p, default_currency="KES")
    assert result.opening_balance == 1000.0
    assert len(result.transactions) == 4
    assert result.check.passed is True


def test_a_wrong_opening_balance_fails_the_check():
    layout = Layout(pre_rows=[["", "Balance brought forward", "", "", "900.00"]])
    p = profile(opening_balance={"source": "table_row", "label": "Balance brought forward"})
    result = read_statement(build(sample_rows(4), layout), p, default_currency="KES")
    assert result.check.passed is False and result.check.failing_rows == [0]
    with pytest.raises(RunningBalanceMismatch):
        read_transactions(build(sample_rows(4), layout), p, default_currency="KES")


def test_a_statement_without_balances_is_checked_against_its_totals():
    headers = ["Date", "Details", "Credit", "Debit"]
    layout = Layout(headers=headers, xs=[40, 110, 360, 460], post_rows=[["", "Total", "340.00", "240.00"]])
    rows = [r[:4] for r in sample_rows(4)]  # in: 100+120, out: 110+130
    p = validate_profile(
        {
            "date_format": "%d/%m/%Y",
            "table": {
                "columns": [
                    {"role": "date", "header": "Date"},
                    {"role": "description", "header": "Details"},
                    {"role": "money_in", "header": "Credit"},
                    {"role": "money_out", "header": "Debit"},
                ],
                "column_bounds": "header_gaps",
            },
            "totals": {"source": "table_row", "label": "Total"},
        }
    )
    bad = read_statement(build(rows, layout), p, default_currency="KES")
    assert bad.totals == (340.0, 240.0) and bad.check.kind == "totals" and bad.check.passed is False
    good_layout = Layout(headers=headers, xs=[40, 110, 360, 460], post_rows=[["", "Total", "220.00", "240.00"]])
    assert read_statement(build(rows, good_layout), p, default_currency="KES").check.passed is True


def test_signed_amounts_with_parentheses_and_comma_decimals():
    layout = Layout(headers=["Date", "Details", "Amount", "Balance"], xs=[40, 110, 360, 460])
    rows = [
        ["01/10/2026", "Salary", "1.000,00", "2.000,00"],
        ["02/10/2026", "Rent", "(500,50)", "1.499,50"],
    ]
    p = validate_profile(
        {
            "date_format": "%d/%m/%Y",
            "amounts": {"decimal_mark": ",", "thousands_sep": ".", "negative": "parentheses"},
            "table": {
                "columns": [
                    {"role": "date", "header": "Date"},
                    {"role": "description", "header": "Details"},
                    {"role": "signed_amount", "header": "Amount"},
                    {"role": "balance", "header": "Balance"},
                ],
                "column_bounds": "header_gaps",
            },
        }
    )
    out = read_transactions(build(rows, layout), p, default_currency="KES")
    assert [(r["direction"], r["amount"], r["balance_after"]) for r in out] == [
        ("in", 1000.0, 2000.0),
        ("out", 500.5, 1499.5),
    ]


def test_dr_suffix_negatives():
    layout = Layout(headers=["Date", "Details", "Amount", "Balance"], xs=[40, 110, 360, 460])
    rows = [["01/10/2026", "Card", "250.00DR", "750.00"], ["02/10/2026", "Refund", "50.00", "800.00"]]
    p = validate_profile(
        {
            "date_format": "%d/%m/%Y",
            "amounts": {"negative": "dr_suffix"},
            "table": {
                "columns": [
                    {"role": "date", "header": "Date"},
                    {"role": "description", "header": "Details"},
                    {"role": "signed_amount", "header": "Amount"},
                    {"role": "balance", "header": "Balance"},
                ],
                "column_bounds": "header_gaps",
            },
        }
    )
    out = read_transactions(build(rows, layout), p, default_currency="KES")
    assert [(r["direction"], r["amount"]) for r in out] == [("out", 250.0), ("in", 50.0)]


def test_newest_first_statements_come_back_oldest_first():
    rows = list(reversed(sample_rows(5)))
    out = read_transactions(build(rows), profile(table={"row_order": "newest_first"}), default_currency="KES")
    assert [r["txn_date"].day for r in out] == [1, 2, 3, 4, 5]


def test_dedupe_hash_with_a_reference_matches_the_existing_parsers():
    layout = Layout(headers=["Ref", "Date", "Details", "Money In", "Money Out", "Balance"], xs=[40, 100, 170, 330, 410, 490])
    rows = [["RX1", *sample_rows(1)[0]]]
    p = validate_profile(
        {
            "date_format": "%d/%m/%Y",
            "table": {
                "columns": [
                    {"role": "reference", "header": "Ref"},
                    {"role": "date", "header": "Date"},
                    {"role": "description", "header": "Details"},
                    {"role": "money_in", "header": "Money In"},
                    {"role": "money_out", "header": "Money Out"},
                    {"role": "balance", "header": "Balance"},
                ],
                "column_bounds": "header_gaps",
            },
        }
    )
    out = read_transactions(build(rows, layout), p, default_currency="KES")
    assert out[0]["dedupe_hash"] == hashlib.sha256(b"RX1|Payment from Shop A|100.00").hexdigest()


def test_dedupe_hash_without_a_reference_is_stable_and_distinct():
    out = read_transactions(build(sample_rows(4)), profile(), default_currency="KES")
    again = read_transactions(build(sample_rows(4)), profile(), default_currency="KES")
    assert [r["dedupe_hash"] for r in out] == [r["dedupe_hash"] for r in again]
    assert len({r["dedupe_hash"] for r in out}) == 4


def test_a_layout_the_profile_doesnt_fit_is_a_parse_error():
    other = Layout(headers=["Posted", "Narrative", "In", "Out", "Running"])
    with pytest.raises(StatementParseError):
        read_transactions(build(sample_rows(3), other), profile(), default_currency="KES")


def test_the_real_mpesa_statement_resolves_with_ruled_columns():
    # Exact parity with the M-Pesa parser is ab-313's job; this only proves
    # the reader finds the table and columns on a real layout.
    fixture = Path(__file__).parent / "fixtures" / "statements" / "mpesa" / "statement_sample.pdf"
    content = decrypt_if_needed(content=fixture.read_bytes(), filename="statement_sample.pdf", password="0000000")
    p = validate_profile(
        {
            "date_format": "%Y-%m-%d %H:%M:%S",
            "table": {
                "columns": [
                    {"role": "reference", "header": "Receipt No"},
                    {"role": "date", "header": "Completion Time"},
                    {"role": "description", "header": "Details"},
                    {"role": "ignore", "header": "Transaction Status"},
                    {"role": "money_in", "header": "Paid In"},
                    {"role": "money_out", "header": "Withdrawn"},
                    {"role": "balance", "header": "Balance"},
                ],
                "column_bounds": "ruled",
                "row_order": "newest_first",
            },
        }
    )
    result = read_statement(content, p, default_currency="KES")
    assert len(result.transactions) > 20
    first = result.transactions[-1]
    assert first["txn_date"] == date(2026, 9, 16)
    assert first["dedupe_hash"]


# --- ab-313: what M-Pesa needs, on synthetic statements -------------------------


def test_a_zero_amount_row_is_kept_in_the_column_it_was_written_in():
    rows = [*sample_rows(2), ["03/10/2026", "Loan request", "0.00", "", "990.00"]]
    out = read_transactions(build(rows), profile(), default_currency="KES")
    assert [(r["direction"], r["amount"]) for r in out][-1] == ("in", 0.0)


def test_rows_with_another_status_are_dropped():
    layout = Layout(
        headers=["Date", "Details", "Status", "Money In", "Money Out", "Balance"], xs=[40, 110, 260, 330, 410, 490]
    )
    rows = [[r[0], r[1], "Completed", *r[2:]] for r in sample_rows(3)]
    rows.insert(1, ["02/10/2026", "Reversed payment", "Failed", "", "999.00", "1,100.00"])
    p = validate_profile(
        {
            "date_format": "%d/%m/%Y",
            "table": {
                "columns": [
                    {"role": "date", "header": "Date"},
                    {"role": "description", "header": "Details"},
                    {"role": "status", "header": "Status"},
                    {"role": "money_in", "header": "Money In"},
                    {"role": "money_out", "header": "Money Out"},
                    {"role": "balance", "header": "Balance"},
                ],
                "column_bounds": "header_gaps",
                "status_keep": "Completed",
                "end_markers": ["End of statement"],
            },
        }
    )
    out = read_transactions(build(rows, layout), p, default_currency="KES")
    assert [r["description"] for r in out] == ["Payment from Shop A", "Paid to Store B", "Payment from Shop C"]


def _tied_rows():
    # Printed newest first; two rows share 02/10 (printed: X then Y).
    return [
        ["03/10/2026", "Third", "", "5.00", "1,000.00"],
        ["02/10/2026", "Tie X", "", "10.00", "1,005.00"],
        ["02/10/2026", "Tie Y", "15.00", "", "1,015.00"],
        ["01/10/2026", "First", "100.00", "", "1,000.00"],
    ]


def test_same_date_rows_reverse_by_default_or_keep_their_printed_order():
    reverse = read_statement(build(_tied_rows()), profile(table={"row_order": "newest_first"}), default_currency="KES")
    assert [r["description"] for r in reverse.transactions] == ["First", "Tie Y", "Tie X", "Third"]
    kept = read_statement(
        build(_tied_rows()), profile(table={"row_order": "newest_first", "tie_order": "as_printed"}), default_currency="KES"
    )
    assert [r["description"] for r in kept.transactions] == ["First", "Tie X", "Tie Y", "Third"]


def test_the_first_tie_is_ordered_by_the_only_ordering_whose_balances_follow():
    rows = [
        ["02/10/2026", "Later", "", "5.00", "1,020.00"],
        ["01/10/2026", "Out", "", "30.00", "1,025.00"],  # printed first, but must come second
        ["01/10/2026", "In", "55.00", "", "1,055.00"],
    ]
    p = profile(table={"row_order": "newest_first", "tie_order": "as_printed", "resolve_first_tie": True})
    out = read_transactions(build(rows), p, default_currency="KES")
    assert [r["description"] for r in out] == ["In", "Out", "Later"]


def test_summary_totals_above_the_table_prove_a_totals_check():
    rows = sample_rows(4)  # in 100 + 120, out 110 + 130
    p = profile(totals={"source": "summary_row", "label": "Total"}, balance_check="totals")
    good = read_statement(build(rows, Layout(name_line_above_table="TOTAL: 220.00 240.00")), p, default_currency="KES")
    assert good.totals == (220.0, 240.0) and good.check.kind == "totals" and good.check.passed
    bad = read_statement(build(rows, Layout(name_line_above_table="TOTAL: 220.00 250.00")), p, default_currency="KES")
    assert bad.check.passed is False
    missing = read_statement(build(rows), p, default_currency="KES")
    assert missing.check.passed is False


def test_a_ruled_table_ends_where_its_rules_end():
    pdf = build(sample_rows(3), Layout(ruled=True, footer="Visit our website for help", end_marker=None))
    out = read_transactions(pdf, profile(table={"column_bounds": "ruled", "end_markers": []}), default_currency="KES")
    assert out[-1]["description"] == "Payment from Shop C"


def test_digit_only_description_lines_can_be_dropped():
    pdf = build(sample_rows(3), continuation={1: "1003642748"})
    kept = read_transactions(pdf, profile(), default_currency="KES")
    assert kept[1]["description"] == "Paid to Store B 1003642748"
    dropped = read_transactions(pdf, profile(table={"drop_digit_only_lines": True}), default_currency="KES")
    assert dropped[1]["description"] == "Paid to Store B"
