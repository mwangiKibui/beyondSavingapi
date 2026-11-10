"""ab-253 PR 3: the preview as a CSV the user can compare with their own
records. Pure - no DB."""

import csv
import io
from datetime import date
from uuid import uuid4

from app.services.merchant_keys import RowKey
from app.services.preview_csv import CSV_COLUMNS, build_preview_csv, safe_cell

GROCERIES = {"id": uuid4(), "name": "Groceries", "type": "expense"}
RENT = {"id": uuid4(), "name": "Rent", "type": "expense"}


def txn(description, *, amount=1000.0, allocated=0.0, category_id=None, categories=0, has_null=False,
        balance_after=5000.0, is_transfer=False):
    return {"id": uuid4(), "txn_date": date(2026, 10, 5), "amount": amount, "currency": "KES", "direction": "out",
            "description": description, "balance_after": balance_after, "is_transfer": is_transfer,
            "allocated": allocated, "category_id": category_id, "category_count": categories,
            "has_uncategorised": has_null}


def parse(text):
    return list(csv.DictReader(io.StringIO(text)))


def test_header_is_the_agreed_columns():
    assert parse(build_preview_csv([], preview_rows=[], categories=[])) == []
    assert build_preview_csv([], preview_rows=[], categories=[]).splitlines()[0] == ",".join(CSV_COLUMNS)


def test_open_row_carries_its_suggestion():
    row = txn("Merchant Payment to 5555555 - NAIVAS")
    preview_row = {"transaction_id": row["id"], "display_name": "NAIVAS", "suggestion": {
        "category_name": "Groceries", "is_new_category": False, "source": "directory", "merchant_name": "Naivas"}}
    out = parse(build_preview_csv([row], preview_rows=[preview_row], categories=[GROCERIES]))[0]
    assert out == {
        "date": "2026-10-05", "description": "Merchant Payment to 5555555 - NAIVAS", "payee": "NAIVAS",
        "direction": "out", "amount": "1000.00", "balance_after": "5000.00", "suggested_category": "Groceries",
        "is_new_category": "no", "suggestion_source": "directory", "matched_merchant": "Naivas", "chosen_category": "",
    }


def test_new_category_and_no_suggestion():
    a, b = txn("SHELL"), txn("SOMEONE")
    rows = [{"transaction_id": a["id"], "display_name": "SHELL", "suggestion": {
                "category_name": "Fuel", "is_new_category": True, "source": "others", "merchant_name": None}},
            {"transaction_id": b["id"], "display_name": "SOMEONE", "suggestion": None}]
    out = parse(build_preview_csv([a, b], preview_rows=rows, categories=[]))
    assert (out[0]["suggested_category"], out[0]["is_new_category"], out[0]["suggestion_source"]) == ("Fuel", "yes", "others")
    assert (out[1]["suggested_category"], out[1]["suggestion_source"]) == ("", "")


def test_already_categorised_rows_show_the_chosen_category():
    single = txn("A", allocated=1000.0, category_id=GROCERIES["id"], categories=1)
    split = txn("B", allocated=1000.0, categories=2)
    none = txn("C", allocated=1000.0, has_null=True)
    partial = txn("D", allocated=400.0, category_id=RENT["id"], categories=1)
    out = parse(build_preview_csv([single, split, none, partial], preview_rows=[], categories=[GROCERIES, RENT]))
    assert [r["chosen_category"] for r in out] == ["Groceries", "Split", "No category", "Rent (partly)"]


def test_transfer_rows_are_marked():
    out = parse(build_preview_csv([txn("TRF", is_transfer=True)], preview_rows=[], categories=[]))
    assert out[0]["chosen_category"] == "Transfer"


def test_missing_balance_is_blank():
    out = parse(build_preview_csv([txn("A", balance_after=None)], preview_rows=[], categories=[]))
    assert out[0]["balance_after"] == ""


def test_cells_that_spreadsheets_would_run_as_formulas_are_escaped():
    for bad in ["=HYPERLINK(\"x\")", "+254700", "-5", "@SUM(A1)", "\tX", "\rX"]:
        assert safe_cell(bad) == "'" + bad
    assert safe_cell("NAIVAS") == "NAIVAS"
    out = parse(build_preview_csv([txn("=cmd|' /C calc'!A0")], preview_rows=[], categories=[]))
    assert out[0]["description"].startswith("'=")


def test_payee_of_a_categorised_row_comes_from_its_key():
    row = txn("IB Outgoing Mobile Payments BuyGoods FTX1 9120385", allocated=1000.0, category_id=GROCERIES["id"],
              categories=1)
    keys = {row["id"]: RowKey(kind="till", value="9120385", display="Till 9120385")}
    out = parse(build_preview_csv([row], preview_rows=[], categories=[GROCERIES], keys=keys))
    assert out[0]["payee"] == "Till 9120385"
