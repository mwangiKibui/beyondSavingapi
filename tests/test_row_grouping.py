"""ab-333: a row's text above or around its date line, the description
check that notices text on the wrong rows, and trying each grouping."""

import pytest

from app.parsers.profiles.reader import (
    ROW_GROUPINGS,
    description_gap_rows,
    read_best_grouping,
    read_statement,
    with_grouping,
)
from app.parsers.profiles.schema import validate_profile
from app.services.layout_proposer import candidates, evaluate, parse_answer
from app.services.statement_layouts import check_layout
from tests.statement_pdfs import Layout, build, sample_rows

PROFILE = {
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
ROWS = sample_rows(30)  # two pages
RIGHT = {"on": {"below", "nearest", "above"}, "above": {"nearest", "above"}, "around": {"nearest"}}


def read(pdf: bytes, grouping: str):
    return read_statement(pdf, with_grouping(validate_profile(PROFILE), grouping), default_currency="KES")


def right_texts(result) -> int:
    return sum(1 for t, src in zip(result.transactions, ROWS, strict=False) if t["description"] == src[1])


@pytest.mark.parametrize("position", ["on", "above", "around"])
@pytest.mark.parametrize("grouping", ROW_GROUPINGS)
def test_a_grouping_passes_only_when_every_row_gets_its_own_text(position, grouping):
    result = read(build(ROWS, Layout(text_position=position)), grouping)
    assert len(result.transactions) == 30
    if grouping in RIGHT[position]:
        assert result.check.passed and right_texts(result) == 30
    else:
        # The balances still add up - only the description check can tell.
        assert not result.check.passed and result.check.kind == "descriptions"
        assert "wrong rows" in result.check.message


@pytest.mark.parametrize("position", ["on", "above", "around"])
def test_the_best_grouping_is_found_from_the_default(position):
    used, result = read_best_grouping(
        build(ROWS, Layout(text_position=position)), validate_profile(PROFILE), default_currency="KES"
    )
    assert used.table.continuation in RIGHT[position]
    assert result.check.passed and right_texts(result) == 30


def test_a_layout_that_reads_keeps_its_own_grouping():
    profile = validate_profile(PROFILE)
    used, _ = read_best_grouping(build(ROWS), profile, default_currency="KES")
    assert used is profile


def test_wrapped_text_under_the_date_still_reads_with_the_default():
    pdf = build(ROWS, continuation={0: "Ref line one", 5: "Ref line two"})
    used, result = read_best_grouping(pdf, validate_profile(PROFILE), default_currency="KES")
    assert used.table.continuation == "below" and result.check.passed
    assert result.transactions[0]["description"].endswith("Ref line one")


def test_rows_without_words_are_description_gaps():
    rows = [{"description": d} for d in ["Shop A", "12345", "", None, "MPS2AB~0712", "x"]]
    assert description_gap_rows(rows) == [1, 2, 3, 5]


def test_too_many_rows_without_a_description_fail_the_read():
    rows = [[*r[:1], "0001" if i % 3 else r[1], *r[2:]] for i, r in enumerate(ROWS)]
    result = read(build(rows), "below")
    assert result.description_gaps == 20
    assert not result.check.passed and result.check.kind == "descriptions"
    assert result.check.message.startswith("20 of 30 rows have no description")


def test_a_couple_of_rows_without_a_description_are_fine():
    rows = [[*r[:1], "0001" if i in (3, 9) else r[1], *r[2:]] for i, r in enumerate(ROWS)]
    result = read(build(rows), "below")
    assert result.check.passed and result.description_gaps == 2


# --- the layout check (wizard) ------------------------------------------------


def test_the_check_reads_with_the_grouping_that_works_and_says_which():
    pdf = build(ROWS, Layout(text_position="above"))
    result = check_layout(pdf, validate_profile(PROFILE), currency="KES")
    assert result.usable and result.row_grouping in RIGHT["above"]
    assert result.profile.table.continuation == result.row_grouping
    assert result.rows[0]["description"] == ROWS[0][1]


def test_a_grouping_the_user_chose_is_used_as_it_is():
    pdf = build(ROWS, Layout(text_position="above"))
    result = check_layout(pdf, validate_profile(PROFILE), currency="KES", grouping_chosen=True)
    assert not result.usable and result.row_grouping == "below"
    assert "wrong rows" in result.message


# --- the AI ---------------------------------------------------------------------

ANSWER = (
    '{"columns": [{"heading": "Date", "role": "date"}, {"heading": "Details", "role": "description"}, '
    '{"heading": "Money In", "role": "money_in"}, {"heading": "Money Out", "role": "money_out"}, '
    '{"heading": "Balance", "role": "balance"}], "date_format": "%d/%m/%Y", "row_grouping": "nearest"}'
)


def test_the_ais_row_grouping_is_tried_first():
    tried = candidates(None, parse_answer(ANSWER), content=build(ROWS, Layout(text_position="around")), file_type="pdf")
    assert tried[0]["table"]["continuation"] == "nearest"


def test_an_answer_without_a_row_grouping_still_finds_the_one_that_reads():
    answer = ANSWER.replace(', "row_grouping": "nearest"', "")
    out = evaluate(build(ROWS, Layout(text_position="around")), None, answer)
    assert out.outcome == "passed" and out.profile["table"]["continuation"] == "nearest"


def test_an_unknown_row_grouping_is_an_invalid_answer():
    assert parse_answer(ANSWER.replace('"nearest"', '"sideways"')) is None
