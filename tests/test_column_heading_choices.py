"""ab-329: column roles learned from what users chose in the mapping wizard."""

from unittest.mock import AsyncMock
from uuid import uuid4

import pytest

from app.repositories.column_heading_choices import (
    learned_roles,
    normalise_heading,
    pick_learned_role,
    record_choices,
)
from app.services.statement_layouts import apply_learned_roles


class Pool:
    def __init__(self, rows=()):
        self.fetch = AsyncMock(return_value=list(rows))
        self.executemany = AsyncMock()


def guess(*columns):
    """A spreadsheet guess with these (header, role) columns."""
    return {
        "file_type": "csv",
        "date_format": "%d/%m/%Y",
        "table": {
            "columns": [{"role": role, "header": header} for header, role in columns],
            "column_bounds": "cells",
        },
    }


def roles_of(profile):
    return [c["role"] for c in profile["table"]["columns"]]


BASIC = [("Date", "date"), ("Details", "description"), ("Money In", "money_in"), ("Money Out", "money_out"), ("Balance", "balance")]


# --- normalising ----------------------------------------------------------------


@pytest.mark.parametrize(
    "text, expected",
    [
        ("Money  In", "money in"),
        ("  PAID OUT: ", "paid out"),
        ("Balance.", "balance"),
        ("", None),
        (None, None),
        (" : ", None),
        ("x" * 41, None),
    ],
)
def test_headings_are_normalised_and_empty_or_long_ones_skipped(text, expected):
    assert normalise_heading(text) == expected


# --- record_choices ---------------------------------------------------------------


async def test_choices_are_upserted_per_user_file_type_and_normalised_heading():
    pool, user = Pool(), uuid4()
    await record_choices(
        pool,
        user_id=user,
        file_type="pdf",
        choices=[("Paid  In:", "money_in", "money_in"), ("Withdrawn", "money_out", None)],
    )
    sql, rows = pool.executemany.await_args.args
    assert "ON CONFLICT (user_id, file_type, heading) DO UPDATE" in sql
    assert "role = EXCLUDED.role" in sql and "suggested_role = EXCLUDED.suggested_role" in sql
    assert "updated_at = now()" in sql
    assert rows == [(user, "pdf", "paid in", "money_in", "money_in"), (user, "pdf", "withdrawn", "money_out", None)]


async def test_headerless_columns_are_skipped_and_nothing_is_written_without_headings():
    pool = Pool()
    await record_choices(pool, user_id=uuid4(), file_type="csv", choices=[(None, "ignore", None), ("  ", "date", None)])
    pool.executemany.assert_not_called()


async def test_the_same_heading_twice_in_one_mapping_is_recorded_once():
    pool, user = Pool(), uuid4()
    await record_choices(
        pool, user_id=user, file_type="csv", choices=[("Amount", "money_in", None), ("AMOUNT", "money_out", None)]
    )
    assert pool.executemany.await_args.args[1] == [(user, "csv", "amount", "money_out", None)]


# --- learned_roles ----------------------------------------------------------------


@pytest.mark.parametrize(
    "votes, expected",
    [
        ({}, None),
        ({"money_in": 2}, None),
        ({"money_in": 3}, "money_in"),
        ({"money_in": 3, "money_out": 2}, "money_in"),
        ({"money_in": 3, "money_out": 3}, None),
        ({"money_in": 5, "money_out": 5, "ignore": 1}, None),
        ({"ignore": 1, "balance": 4}, "balance"),
    ],
)
def test_a_role_is_learned_from_three_users_and_a_clear_lead(votes, expected):
    assert pick_learned_role(votes) == expected


async def test_learned_roles_count_distinct_users_per_role():
    pool = Pool(
        rows=[
            {"heading": "paid in", "role": "money_in", "users": 3},
            {"heading": "paid in", "role": "money_out", "users": 1},
            {"heading": "withdrawn", "role": "money_out", "users": 2},
            {"heading": "ref", "role": "reference", "users": 4},
            {"heading": "ref", "role": "ignore", "users": 4},
        ]
    )
    learned = await learned_roles(pool, file_type="pdf", headings=["Paid In:", "Withdrawn", "Ref", None, ""])
    assert learned == {"Paid In:": "money_in"}
    sql, file_type, headings = pool.fetch.await_args.args
    assert "count(DISTINCT user_id)" in sql and "GROUP BY heading, role" in sql
    assert file_type == "pdf" and headings == ["paid in", "ref", "withdrawn"]


async def test_no_headings_means_no_lookup():
    pool = Pool()
    assert await learned_roles(pool, file_type="pdf", headings=[None, " "]) == {}
    pool.fetch.assert_not_called()


# --- apply_learned_roles ------------------------------------------------------------


def test_learned_roles_replace_the_keyword_ones():
    out, sources = apply_learned_roles(guess(*BASIC), {"Money In": "money_out", "Money Out": "money_in"})
    assert roles_of(out) == ["date", "description", "money_out", "money_in", "balance"]
    assert sources == ["keyword", "keyword", "learned", "learned", "keyword"]


def test_nothing_learned_keeps_the_guess():
    original = guess(*BASIC)
    out, sources = apply_learned_roles(original, {})
    assert out is original and sources == ["keyword"] * 5


def test_a_learned_role_agreeing_with_the_keyword_counts_as_learned():
    _, sources = apply_learned_roles(guess(*BASIC), {"Balance": "balance"})
    assert sources == ["keyword", "keyword", "keyword", "keyword", "learned"]


def test_a_single_use_role_keeps_only_its_first_column_after_learning():
    original = guess(("Date", "date"), ("Posted", "ignore"), *BASIC[1:])
    out, sources = apply_learned_roles(original, {"Posted": "date"})
    assert roles_of(out) == ["date", "ignore", "description", "money_in", "money_out", "balance"]
    assert sources[1] == "keyword"

    out, sources = apply_learned_roles(original, {"Date": "ignore", "Posted": "date"})
    assert roles_of(out)[:2] == ["ignore", "date"] and sources[:2] == ["learned", "learned"]


def test_a_signed_amount_gives_way_to_money_in_and_out_after_learning():
    original = guess(("Date", "date"), ("Details", "description"), ("Amount", "ignore"), *BASIC[2:])
    out, sources = apply_learned_roles(original, {"Amount": "signed_amount"})
    assert roles_of(out)[2] == "ignore" and sources[2] == "keyword"


def test_learned_roles_that_dont_make_a_valid_layout_keep_the_guess():
    original = guess(*BASIC)
    out, sources = apply_learned_roles(original, {"Details": "reference"})  # no description left
    assert out is original and sources == ["keyword"] * 5
