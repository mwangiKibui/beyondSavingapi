"""ab-314: the AI layout proposer. Synthetic PDFs; the model is faked, so
nothing is sent anywhere."""

import json
from unittest.mock import AsyncMock
from uuid import uuid4

import pytest

from app.parsers.profiles.guess import layout_start
from app.services import layout_proposer
from app.services.inference import InferenceError, InferenceReply
from app.services.layout_proposer import candidates, evaluate, parse_answer, propose_layout
from tests.statement_pdfs import Layout, build

ODD = ["When", "What", "Plus", "Minus", "Left"]
ROWS = [
    ["01/10/2026", "Payment from Shop A", "100.00", "", "1,100.00"],
    ["02/10/2026", "Paid to Store B", "", "40.00", "1,060.00"],
    ["03/10/2026", "Payment from Shop C", "200.00", "", "1,260.00"],
]
RIGHT = {
    "columns": [
        {"heading": "When", "role": "date"},
        {"heading": "What", "role": "description"},
        {"heading": "Plus", "role": "money_in"},
        {"heading": "Minus", "role": "money_out"},
        {"heading": "Left", "role": "balance"},
    ],
    "date_format": "%d/%m/%Y",
    "opening_balance_label": None,
    "totals_in_label": None,
    "totals_out_label": None,
}


def odd_pdf() -> bytes:
    return build(ROWS, Layout(headers=ODD))


def odd_guess(pdf: bytes) -> dict:
    return layout_start(pdf, "pdf")["guess"]


def reply(answer: dict) -> InferenceReply:
    return InferenceReply(
        content=json.dumps(answer), model="openai-gpt-oss-120b", prompt_tokens=300, completion_tokens=400
    )


# --- the answer ---------------------------------------------------------------


def test_parse_answer_finds_the_json_in_chatty_text():
    answer = parse_answer("Sure! Here it is:\n" + json.dumps(RIGHT) + "\nHope that helps.")
    assert answer is not None and [c.role for c in answer.columns][:2] == ["date", "description"]


@pytest.mark.parametrize(
    "text",
    ["no json here", '{"columns": []}', '{"columns": [{"heading": "When", "role": "amount_maybe"}, {"heading": "x", "role": "date"}]}', "{not json"],
)
def test_parse_answer_rejects_anything_else(text):
    assert parse_answer(text) is None


def test_the_answer_is_merged_onto_the_guess_by_heading():
    pdf = odd_pdf()
    tried = candidates(odd_guess(pdf), parse_answer(json.dumps(RIGHT)))
    assert [c["role"] for c in tried[0]["table"]["columns"]] == ["date", "description", "money_in", "money_out", "balance"]
    # Geometry comes from our guess, not from the model.
    assert tried[0]["table"]["column_bounds"] == odd_guess(pdf)["table"]["column_bounds"]


def test_a_column_without_a_stored_heading_takes_the_role_in_its_position():
    rows = ROWS + [["04/10/2026", "Paid to Store D", "", "60.00", "1,200.00"]]
    pdf = build(rows, Layout(headers=["Posted", "Narr.", "Cr", "Dr", "Bal"]))
    guess = layout_start(pdf, "pdf")["guess"]
    assert guess["table"]["columns"][1].get("header") is None  # "Narr." isn't stored
    answer = {**RIGHT, "columns": [{"heading": h, "role": r["role"]} for h, r in zip(["Posted", "Narr.", "Cr", "Dr", "Bal"], RIGHT["columns"])]}
    result = evaluate(pdf, guess, json.dumps(answer))
    assert result.outcome == "passed"
    assert [c["role"] for c in result.profile["table"]["columns"]] == ["date", "description", "money_in", "money_out", "balance"]


def test_a_correct_answer_passes_the_balance_check():
    pdf = odd_pdf()
    result = evaluate(pdf, odd_guess(pdf), json.dumps(RIGHT))
    assert result.outcome == "passed"
    assert result.profile["date_format"] == "%d/%m/%Y"


def test_newest_first_rows_are_found_without_asking_the_model():
    newest = list(reversed(ROWS))
    pdf = build(newest, Layout(headers=ODD))
    result = evaluate(pdf, odd_guess(pdf), json.dumps(RIGHT))
    assert result.outcome == "passed"
    assert result.profile["table"]["row_order"] == "newest_first"


def test_a_wrong_answer_fails_the_check_and_cant_invent_amounts():
    pdf = odd_pdf()
    wrong = {**RIGHT, "columns": [{**c, "role": {"money_in": "money_out", "money_out": "money_in"}.get(c["role"], c["role"])} for c in RIGHT["columns"]]}
    result = evaluate(pdf, odd_guess(pdf), json.dumps(wrong))
    assert result.outcome == "failed_check"


def test_an_unusable_answer_is_invalid():
    pdf = odd_pdf()
    assert evaluate(pdf, odd_guess(pdf), "I can't tell").outcome == "invalid"
    # Roles that don't make a profile (no amount column at all).
    no_amounts = {**RIGHT, "columns": [{"heading": h, "role": "ignore"} for h in ODD]}
    assert evaluate(pdf, odd_guess(pdf), json.dumps(no_amounts)).outcome == "invalid"


# --- the worker step ----------------------------------------------------------


@pytest.fixture
def ai_on(monkeypatch):
    monkeypatch.setattr(layout_proposer, "inference_enabled", lambda: True)
    inserted = AsyncMock()
    monkeypatch.setattr(layout_proposer, "insert_proposal", inserted)
    return inserted


def record():
    return {"id": uuid4(), "user_id": uuid4(), "provider": "Airtel Money"}


async def test_a_passing_proposal_is_recorded(ai_on, monkeypatch):
    sent = {}

    async def fake_complete(prompt, **_):
        sent["prompt"] = prompt
        return reply(RIGHT)

    monkeypatch.setattr(layout_proposer, "complete", fake_complete)
    await propose_layout(object(), record=record(), content=odd_pdf(), file_type="pdf")
    kwargs = ai_on.await_args.kwargs
    assert kwargs["outcome"] == "passed"
    assert kwargs["profile"]["table"]["columns"][2]["role"] == "money_in"
    assert (kwargs["prompt_tokens"], kwargs["completion_tokens"]) == (300, 400)
    # Only the masked page went out: headings yes, values no.
    assert "When" in sent["prompt"] and "Minus" in sent["prompt"]
    for value in ["Shop A", "Store B", "1,100.00", "01/10/2026"]:
        assert value not in sent["prompt"]


async def test_no_ai_when_our_guess_already_passes(ai_on, monkeypatch):
    called = AsyncMock()
    monkeypatch.setattr(layout_proposer, "complete", called)
    pdf = build(ROWS, Layout())  # Date / Details / Money In / Money Out / Balance
    await propose_layout(object(), record=record(), content=pdf, file_type="pdf")
    called.assert_not_awaited()
    ai_on.assert_not_awaited()


async def test_no_ai_without_a_key(monkeypatch):
    monkeypatch.setattr(layout_proposer, "inference_enabled", lambda: False)
    called = AsyncMock()
    monkeypatch.setattr(layout_proposer, "complete", called)
    await propose_layout(object(), record=record(), content=odd_pdf(), file_type="pdf")
    called.assert_not_awaited()


async def test_no_ai_over_the_daily_limit(ai_on, limits, monkeypatch):
    limits(5, used=5)
    called = AsyncMock()
    monkeypatch.setattr(layout_proposer, "complete", called)
    await propose_layout(object(), record=record(), content=odd_pdf(), file_type="pdf")
    called.assert_not_awaited()
    ai_on.assert_not_awaited()


async def test_a_failed_call_is_recorded_and_never_raises(ai_on, monkeypatch):
    monkeypatch.setattr(layout_proposer, "complete", AsyncMock(side_effect=InferenceError("timeout")))
    await propose_layout(object(), record=record(), content=odd_pdf(), file_type="pdf")
    assert ai_on.await_args.kwargs["outcome"] == "error"


async def test_anything_unexpected_never_raises(ai_on, monkeypatch):
    monkeypatch.setattr(layout_proposer, "layout_start", lambda *a: (_ for _ in ()).throw(RuntimeError("boom")))
    await propose_layout(object(), record=record(), content=odd_pdf(), file_type="pdf")


# --- no table found by our guess (ab-314 follow-up) ------------------------------

DOTTED = [
    ["01.Oct.2026", "Payment from Shop A", "100.00", "", "1,100.00"],
    ["02.Oct.2026", "Paid to Store B", "", "40.00", "1,060.00"],
    ["03.Oct.2026", "Payment from Shop C", "200.00", "", "1,260.00"],
]


def dotted_pdf() -> bytes:
    # A date format we don't know, so our guess finds no table.
    return build(DOTTED, Layout(headers=ODD))


def test_with_no_guess_the_answer_builds_the_table_from_its_headings():
    pdf = dotted_pdf()
    assert odd_guess(pdf) is None
    result = evaluate(pdf, None, json.dumps({**RIGHT, "date_format": "%d.%b.%Y"}))
    assert result.outcome == "passed"
    table = result.profile["table"]
    assert table["column_bounds"] == "header_gaps"
    assert [(c["header"], c["role"]) for c in table["columns"]] == [(c["heading"], c["role"]) for c in RIGHT["columns"]]


def test_the_other_date_column_is_tried_when_the_answer_picks_the_wrong_one():
    headers = ["Date", "Value Date", "Details", "Money In", "Money Out", "Balance"]
    # The first date column holds codes on some rows; the value date is the real one.
    rows = [
        ["REF-0001", "01/10/2026", "Payment from Shop A", "100.00", "", "1,100.00"],
        ["02/10/2026", "02/10/2026", "Paid to Store B", "", "40.00", "1,060.00"],
        ["REF-0003", "03/10/2026", "Payment from Shop C", "200.00", "", "1,260.00"],
    ]
    pdf = build(rows, Layout(headers=headers, xs=[40, 105, 170, 330, 410, 490]))
    answer = {
        **RIGHT,
        "columns": [{"heading": h, "role": r} for h, r in zip(headers, ["date", "ignore", "description", "money_in", "money_out", "balance"])],
    }
    result = evaluate(pdf, None, json.dumps(answer))
    assert result.outcome == "passed"
    assert [c["role"] for c in result.profile["table"]["columns"]][:2] == ["ignore", "date"]


def test_a_failed_answer_with_no_guess_is_still_kept_as_a_suggestion():
    pdf = dotted_pdf()
    wrong = {**RIGHT, "date_format": "%d.%b.%Y", "columns": [{**c, "role": {"money_in": "money_out", "money_out": "money_in"}.get(c["role"], c["role"])} for c in RIGHT["columns"]]}
    result = evaluate(pdf, None, json.dumps(wrong))
    assert result.outcome == "failed_check"
    assert [c["header"] for c in result.profile["table"]["columns"]] == ODD


async def test_the_ai_runs_when_our_guess_finds_no_table(ai_on, monkeypatch):
    monkeypatch.setattr(layout_proposer, "complete", AsyncMock(return_value=reply({**RIGHT, "date_format": "%d.%b.%Y"})))
    await propose_layout(object(), record=record(), content=dotted_pdf(), file_type="pdf")
    assert ai_on.await_args.kwargs["outcome"] == "passed"
