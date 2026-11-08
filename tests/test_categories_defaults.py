from unittest.mock import AsyncMock
from uuid import uuid4

from app.repositories.categories import DEFAULT_CATEGORIES, seed_default_categories

# ab-258: added so common Kenyan payments (M-Pesa/bank charges, KRA and
# eCitizen fees, NSSF/SHA, insurance premiums, books, betting payouts)
# have a default category the merchant directory can map to.
NEW_IN_AB_258 = [
    ("Bank & mobile charges", "expense"),
    ("Taxes & government fees", "expense"),
    ("Statutory contributions", "expense"),
    ("Insurance", "expense"),
    ("Books & stationery", "expense"),
    ("Winnings", "income"),
]


def test_default_categories_has_26_entries():
    assert len(DEFAULT_CATEGORIES) == 26


def test_default_categories_include_ab_258_additions():
    for entry in NEW_IN_AB_258:
        assert entry in DEFAULT_CATEGORIES


def test_default_categories_keep_the_original_20():
    # The ab-33 set stays first and unchanged; the new ones are appended.
    assert DEFAULT_CATEGORIES[:20][0] == ("Rent", "expense")
    assert DEFAULT_CATEGORIES[:20][-1] == ("Gifts Received", "income")
    assert DEFAULT_CATEGORIES[20:] == NEW_IN_AB_258


def test_default_categories_have_no_case_insensitive_duplicates():
    keys = [(name.lower(), category_type) for name, category_type in DEFAULT_CATEGORIES]
    assert len(keys) == len(set(keys))


def test_default_categories_types_are_valid():
    assert {category_type for _, category_type in DEFAULT_CATEGORIES} == {"expense", "income"}


async def test_seed_default_categories_inserts_all_26_for_the_user():
    conn = AsyncMock()
    user_id = uuid4()

    await seed_default_categories(conn, user_id=user_id)

    conn.executemany.assert_awaited_once()
    sql, rows = conn.executemany.await_args.args
    assert "INSERT INTO categories" in sql
    assert len(rows) == 26
    assert all(row[0] == user_id for row in rows)
    assert (user_id, "Winnings", "income") in rows
    assert (user_id, "Bank & mobile charges", "expense") in rows
