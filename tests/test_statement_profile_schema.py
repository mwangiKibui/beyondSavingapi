"""ab-312: the statement profile format - layout only, never cell values."""

import copy

import pytest

from app.parsers.profiles.schema import InvalidStatementProfile, layout_texts, validate_profile


def profile(**overrides) -> dict:
    """An M-Pesa-like profile: ruled table, money in/out, balance, reference."""
    base = {
        "schema_version": 1,
        "file_type": "pdf",
        "currency": None,
        "date_format": "%Y-%m-%d %H:%M:%S",
        "amounts": {"decimal_mark": ".", "thousands_sep": ",", "negative": "minus"},
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
        "opening_balance": {"source": "first_row"},
        "totals": {"source": "none"},
    }
    for key, value in overrides.items():
        base[key] = value
    return base


def with_table(**changes) -> dict:
    p = profile()
    p["table"] = {**p["table"], **changes}
    return p


def rejected(raw: dict) -> str:
    with pytest.raises(InvalidStatementProfile) as exc:
        validate_profile(raw)
    return str(exc.value)


# --- valid profiles -------------------------------------------------------------


def test_a_ruled_money_in_and_out_profile_is_valid():
    p = validate_profile(profile())
    assert [c.role for c in p.table.columns][:3] == ["reference", "date", "description"]
    assert p.table.header_lines == 1
    assert p.table.skip_page_numbers is True


def test_a_signed_amount_profile_with_explicit_bounds_is_valid():
    p = validate_profile(
        with_table(
            columns=[
                {"role": "date", "x0": 0.0, "x1": 0.2},
                {"role": "description", "x0": 0.2, "x1": 0.7},
                {"role": "signed_amount", "x0": 0.7, "x1": 0.85},
                {"role": "balance", "x0": 0.85, "x1": 1.0},
            ],
            column_bounds="explicit",
        )
        | {"amounts": {"decimal_mark": ",", "thousands_sep": ".", "negative": "parentheses"}}
    )
    assert p.signed_amount_positive_is == "in"


def test_a_profile_without_a_balance_column_is_valid_when_totals_are_checked():
    raw = with_table(
        columns=[
            {"role": "date", "header": "Date"},
            {"role": "description", "header": "Details"},
            {"role": "money_in", "header": "Credit"},
            {"role": "money_out", "header": "Debit"},
        ]
    )
    raw["totals"] = {"source": "table_row", "label": "Total"}
    assert validate_profile(raw).totals.label == "Total"


def test_layout_texts_lists_every_word_the_profile_stores():
    raw = profile()
    raw["table"]["end_markers"] = ["Disclaimer"]
    raw["table"]["skip_rows"] = [{"match": "starts_with", "text": "Brought forward"}]
    texts = layout_texts(validate_profile(raw))
    assert {"Receipt No", "Paid In", "Disclaimer", "Brought forward"} <= set(texts)


# --- refused: anything that could hold someone's data -------------------------


@pytest.mark.parametrize(
    "header",
    ["Acc 0712345678", "JANE@DOE", "Paid In +", "Ref #12", "x" * 41, "one two three four five six"],
)
def test_layout_text_with_digits_symbols_or_too_long_is_refused(header):
    raw = profile()
    raw["table"]["columns"][2]["header"] = header
    rejected(raw)


def test_an_unknown_key_is_refused_at_every_level():
    for path in [(), ("table",), ("amounts",), ("opening_balance",)]:
        raw = profile()
        target = raw
        for key in path:
            target = target[key]
        target["account_holder"] = "Someone"
        rejected(raw)
    raw = profile()
    raw["table"]["columns"][0]["value"] = "x"
    rejected(raw)


def test_free_patterns_are_not_part_of_the_format():
    raw = profile()
    raw["table"]["skip_rows"] = [{"match": "regex", "text": "Total"}]
    rejected(raw)


def test_an_oversize_profile_is_refused():
    raw = profile()
    raw["table"]["skip_rows"] = [{"match": "contains", "text": "a" * 40}] * 400
    assert "too large" in rejected(raw)


# --- refused: profiles that can't be read or checked --------------------------


@pytest.mark.parametrize(
    "date_format",
    ["%Y-%m-%d %Q", "%d/%m", "Date %d/%m/%Y", "", "%d/%m/%Y" + " " * 30],
)
def test_bad_date_formats_are_refused(date_format):
    rejected(profile(date_format=date_format))


def test_a_profile_needs_exactly_one_date_and_one_description_column():
    raw = profile()
    raw["table"]["columns"][1]["role"] = "description"  # two descriptions, no date
    rejected(raw)


def test_a_signed_amount_cant_be_mixed_with_money_in_and_out():
    raw = profile()
    raw["table"]["columns"][3] = {"role": "signed_amount", "header": "Amount"}
    rejected(raw)


def test_money_in_needs_money_out():
    raw = profile()
    raw["table"]["columns"][5]["role"] = "ignore"
    rejected(raw)


def test_a_profile_with_no_way_to_check_balances_is_refused():
    raw = profile()
    raw["table"]["columns"][6]["role"] = "ignore"  # no balance column, totals "none"
    assert "check" in rejected(raw)


def test_ruled_and_header_columns_need_a_header():
    raw = profile()
    raw["table"]["columns"][2]["header"] = None
    rejected(raw)


def test_explicit_bounds_must_be_ordered_and_not_overlap():
    rejected(
        with_table(
            columns=[
                {"role": "date", "x0": 0.0, "x1": 0.3},
                {"role": "description", "x0": 0.25, "x1": 0.6},
                {"role": "money_in", "x0": 0.6, "x1": 0.7},
                {"role": "money_out", "x0": 0.7, "x1": 0.8},
                {"role": "balance", "x0": 0.8, "x1": 1.0},
            ],
            column_bounds="explicit",
        )
    )


def test_a_table_row_source_needs_a_label():
    raw = profile()
    raw["opening_balance"] = {"source": "table_row"}
    rejected(raw)


def test_matching_decimal_and_thousands_marks_are_refused():
    rejected(profile(amounts={"decimal_mark": ".", "thousands_sep": ".", "negative": "minus"}))


def test_validation_doesnt_change_the_input():
    raw = profile()
    before = copy.deepcopy(raw)
    validate_profile(raw)
    assert raw == before


# --- ab-313 additions -----------------------------------------------------------


def test_a_status_column_with_a_kept_status_is_valid():
    raw = profile()
    raw["table"]["columns"][3]["role"] = "status"
    raw["table"]["status_keep"] = "Completed"
    assert validate_profile(raw).table.status_keep == "Completed"


def test_status_keep_needs_a_status_column():
    raw = profile()
    raw["table"]["status_keep"] = "Completed"
    rejected(raw)


def test_a_totals_check_needs_totals_to_check_against():
    rejected(profile(balance_check="totals"))
    raw = profile(balance_check="totals")
    raw["totals"] = {"source": "summary_row", "label": "Total"}
    assert validate_profile(raw).balance_check == "totals"


def test_a_summary_row_needs_a_label():
    raw = profile()
    raw["totals"] = {"source": "summary_row"}
    rejected(raw)


# --- ab-319: CSV and XLSX -----------------------------------------------------------


def test_a_csv_profile_uses_cell_columns():
    raw = with_table(column_bounds="cells")
    raw["file_type"] = "csv"
    assert validate_profile(raw).table.column_bounds == "cells"
    xlsx = with_table(column_bounds="cells", sheet=1)
    xlsx["file_type"] = "xlsx"
    assert validate_profile(xlsx).table.sheet == 1


def test_cells_and_spreadsheet_types_go_together():
    rejected(with_table(column_bounds="cells"))  # a PDF with cell columns
    raw = profile(file_type="csv")  # a CSV with ruled columns
    rejected(raw)


def test_a_spreadsheet_heading_is_one_row():
    raw = with_table(column_bounds="cells", header_lines=2)
    raw["file_type"] = "xlsx"
    rejected(raw)
