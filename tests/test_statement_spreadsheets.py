"""ab-319: the shared reader over CSV and XLSX statements."""

from datetime import date

import pytest

from app.parsers.base import StatementParseError
from app.parsers.profiles import tabular
from app.parsers.profiles.reader import read_statement, read_transactions
from app.parsers.profiles.schema import validate_profile
from tests.statement_sheets import HEADER, PREAMBLE, csv_bytes, rows, xlsx_bytes


def profile(file_type="csv", **changes):
    raw = {
        "file_type": file_type,
        "date_format": "%d/%m/%Y",
        "table": {
            "columns": [
                {"role": "date", "header": "Date"},
                {"role": "description", "header": "Details"},
                {"role": "money_in", "header": "Money In"},
                {"role": "money_out", "header": "Money Out"},
                {"role": "balance", "header": "Balance"},
            ],
            "column_bounds": "cells",
        },
    }
    for key, value in changes.items():
        if key == "table":
            raw["table"] = {**raw["table"], **value}
        else:
            raw[key] = value
    return validate_profile(raw)


GRID = [*PREAMBLE, HEADER, *rows(4)]


def _check(out):
    assert len(out) == 4
    assert out[0]["txn_date"] == date(2026, 10, 1)
    assert (out[0]["direction"], out[0]["amount"], out[0]["balance_after"]) == ("in", 100.0, 1100.0)
    assert (out[1]["direction"], out[1]["amount"], out[1]["balance_after"]) == ("out", 110.0, 990.0)
    assert out[1]["description"] == "Paid to Store B"


def test_a_csv_statement_reads_below_its_preamble():
    _check(read_transactions(csv_bytes(GRID), profile(), default_currency="KES"))


def test_a_semicolon_csv_with_comma_decimals_in_latin_1():
    content = csv_bytes([*PREAMBLE, HEADER, *rows(4)], delimiter=";", encoding="latin-1", decimal=",")
    p = profile(amounts={"decimal_mark": ",", "thousands_sep": " ", "negative": "minus"})
    _check(read_transactions(content, p, default_currency="KES"))


def test_an_xlsx_statement_with_real_date_and_number_cells():
    _check(read_transactions(xlsx_bytes(GRID), profile("xlsx"), default_currency="KES"))


def test_an_xlsx_sheet_other_than_the_first():
    content = xlsx_bytes(GRID, sheets=2)
    _check(read_transactions(content, profile("xlsx", table={"sheet": 1}), default_currency="KES"))
    with pytest.raises(StatementParseError, match="sheet"):
        read_transactions(xlsx_bytes(GRID), profile("xlsx", table={"sheet": 3}), default_currency="KES")


def test_columns_are_found_by_heading_in_any_position():
    swapped = [[r[1], r[0], r[4], r[2], r[3]] for r in [HEADER, *rows(4)]]
    _check(read_transactions(csv_bytes(swapped), profile(), default_currency="KES"))


def test_newest_first_and_a_wrong_balance_fails_the_check():
    newest = [HEADER, *reversed(rows(4))]
    out = read_transactions(csv_bytes(newest), profile(table={"row_order": "newest_first"}), default_currency="KES")
    assert [r["txn_date"].day for r in out] == [1, 2, 3, 4]
    bad = rows(4)
    bad[2][4] = 5.0
    result = read_statement(csv_bytes([HEADER, *bad]), profile(), default_currency="KES")
    assert result.check.passed is False and 2 in result.check.failing_rows


def test_a_summary_total_above_the_table_proves_a_totals_check():
    grid = [["Total", 220.0, 240.0], [], HEADER, *rows(4)]
    p = profile("xlsx", totals={"source": "summary_row", "label": "Total"}, balance_check="totals")
    result = read_statement(xlsx_bytes(grid), p, default_currency="KES")
    assert result.totals == (220.0, 240.0) and result.check.passed


def test_end_markers_and_blank_rows():
    grid = [HEADER, rows(4)[0], [], *rows(4)[1:], ["End of statement"], ["01/11/2026", "After the end", 5.0, None, 9.0]]
    p = profile("xlsx", table={"end_markers": ["End of statement"]})
    assert len(read_transactions(xlsx_bytes(grid), p, default_currency="KES")) == 4


def test_headings_not_found_is_a_parse_error():
    with pytest.raises(StatementParseError, match="headings"):
        read_transactions(csv_bytes([["Posted", "Narrative"], ["01/10/2026", "x"]]), profile(), default_currency="KES")


def test_too_many_rows_is_refused(monkeypatch):
    monkeypatch.setattr(tabular, "MAX_ROWS", 5)
    with pytest.raises(StatementParseError, match="more than"):
        read_transactions(csv_bytes([HEADER, *rows(8)]), profile(), default_currency="KES")


def test_formulas_are_never_evaluated_only_cached_values_are_read():
    # A formula cell openpyxl wrote has no cached value: it reads as empty.
    grid = [HEADER, *rows(2)]
    grid[1][1] = '=HYPERLINK("http://example.com","Shop")'
    out = read_statement(xlsx_bytes(grid), profile("xlsx"), default_currency="KES")
    assert all("HYPERLINK" not in (t["description"] or "") for t in out.transactions)
