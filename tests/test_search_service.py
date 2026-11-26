"""ab-274: the pure parts of the navbar search (app/services/search.py)."""

from datetime import date, datetime, timezone
from decimal import Decimal
from uuid import uuid4

import pytest

from app.services.search import (
    account_item,
    like_pattern,
    normalise_query,
    parse_amount,
    payee_items,
    plan_item,
    transaction_item,
)

SECRET = "test-secret"
GROCER = "Merchant Payment to 5555555 - ACME SUPERMARKET"
PERSON = "Customer Transfer to 0700000000 - A N OTHER"


def row(description, *, txn_date=date(2026, 10, 1), direction="out", provider="M-Pesa", amount="100.00"):
    return {"id": uuid4(), "txn_date": txn_date, "amount": Decimal(amount), "currency": "KES",
            "direction": direction, "description": description, "provider": provider}


# --- query handling -----------------------------------------------------------

@pytest.mark.parametrize("raw, expected", [
    ("  java  ", "java"),
    ("java   house", "java house"),
    ("ab", "ab"),
    ("a", None),
    ("   a  ", None),
    ("", None),
    (None, None),
])
def test_normalise_query(raw, expected):
    assert normalise_query(raw) == expected


@pytest.mark.parametrize("raw, expected", [
    ("2600", Decimal("2600")),
    ("2,600", Decimal("2600")),
    ("2,600.50", Decimal("2600.50")),
    ("KES 2,600", Decimal("2600")),
    ("ksh2600", Decimal("2600")),
    ("1 000", Decimal("1000")),
    ("abc", None),
    ("12-34", None),
    ("2.6.0", None),
    ("KES", None),
    ("1e3", None),
    ("NaN", None),
])
def test_parse_amount(raw, expected):
    assert parse_amount(raw) == expected


def test_like_pattern_escapes_wildcards():
    assert like_pattern("50% off_now\\") == "%50\\%%off\\_now\\\\%"
    assert like_pattern("java") == "%java%"


def test_like_pattern_allows_any_gap_between_words():
    assert like_pattern("java house") == "%java%house%"


# --- payees -------------------------------------------------------------------

def test_payee_rows_of_one_till_form_one_group():
    rows = [row(GROCER, txn_date=date(2026, 10, 1)), row(GROCER, txn_date=date(2026, 11, 3))]

    items, total = payee_items(rows, q="acme", secret=SECRET, limit=5, offset=0)

    assert total == 1
    [item] = items
    assert item["type"] == "payee"
    assert item["id"].startswith("g:")
    assert item["title"] == "ACME SUPERMARKET"
    assert item["subtitle"] == "2 transactions · last 3 Nov 2026"
    assert item["row_count"] == 2 and item["is_person"] is False
    assert item["link"] == {"group_key": item["id"], "direction": "out", "search": "ACME SUPERMARKET"}


def test_payee_person_row_shows_statement_text_and_links_by_query():
    items, total = payee_items([row(PERSON)], q="other", secret=SECRET, limit=5, offset=0)

    assert total == 1
    assert items[0]["is_person"] is True
    assert items[0]["title"] == "A N OTHER · 2547•••000"  # ab-341
    assert items[0]["subtitle"] == "1 transaction · last 1 Oct 2026"
    # A person's name never becomes a reusable key; the user's own words do.
    assert items[0]["link"]["search"] == "other"


NCBA_IN = "MPESA Paybill Transfer 1000000001 GRACE MPESA REF: {ref} JANE DOE 4c68f3c108f7 FTC2608{ref}"


def test_payee_link_text_is_found_in_every_row_of_the_payee():
    # The display name drops references from the middle of the text, so it
    # isn't in the descriptions; Transactions searches by substring, so the
    # link carries the longest text all of the payee's rows share.
    rows = [row(NCBA_IN.format(ref=ref), direction="in", provider="NCBA Bank")
            for ref in ("UHOJH3QNMJ", "UHHJH2Y0MX", "UHHJH2XXX6")]

    [item], _ = payee_items(rows, q="grace", secret=SECRET, limit=5, offset=0)

    search = item["link"]["search"]
    assert all(search.lower() in r["description"].lower() for r in rows)
    assert search == "MPESA Paybill Transfer 1000000001 GRACE MPESA REF: UH"


def test_rows_without_a_payee_are_not_payees():
    items, total = payee_items([row("AGENT DEPOSIT 123")], q="agent", secret=SECRET, limit=5, offset=0)
    assert (items, total) == ([], 0)


def test_money_in_and_out_from_one_payee_are_separate_groups():
    rows = [row(GROCER), row(GROCER, direction="in")]
    _, total = payee_items(rows, q="acme", secret=SECRET, limit=5, offset=0)
    assert total == 2


def test_payees_are_newest_first_and_paged():
    rows = [
        row("Merchant Payment to 1111111 - FIRST SHOP", txn_date=date(2026, 9, 1)),
        row("Merchant Payment to 2222222 - SECOND SHOP", txn_date=date(2026, 11, 1)),
        row("Merchant Payment to 3333333 - THIRD SHOP", txn_date=date(2026, 10, 1)),
    ]

    first, total = payee_items(rows, q="shop", secret=SECRET, limit=2, offset=0)
    second, _ = payee_items(rows, q="shop", secret=SECRET, limit=2, offset=2)

    assert total == 3
    assert [i["title"] for i in first] == ["SECOND SHOP", "THIRD SHOP"]
    assert [i["title"] for i in second] == ["FIRST SHOP"]


# --- other item types -----------------------------------------------------------

def test_transaction_item_out():
    tid, aid = uuid4(), uuid4()
    item = transaction_item({
        "id": tid, "account_id": aid, "account_nickname": "My M-Pesa", "txn_date": date(2026, 11, 3),
        "amount": Decimal("2600.00"), "currency": "KES", "direction": "out",
        "description": "Merchant  Payment to 5555555 - ACME SUPERMARKET", "counterparty": None,
    })
    assert item == {
        "type": "transaction", "id": str(tid), "title": "Merchant Payment to 5555555 - ACME SUPERMARKET",
        "subtitle": "3 Nov 2026 · My M-Pesa · −KES 2,600.00",
        "link": {"transaction_id": str(tid), "account_id": str(aid)},
        "txn_date": date(2026, 11, 3), "amount": 2600.0, "currency": "KES", "direction": "out",
        "account_nickname": "My M-Pesa",
    }


def test_transaction_item_in_without_description_uses_counterparty():
    item = transaction_item({
        "id": uuid4(), "account_id": uuid4(), "account_nickname": "Bank", "txn_date": date(2026, 1, 5),
        "amount": Decimal("10"), "currency": "KES", "direction": "in", "description": None,
        "counterparty": "ACME LTD",
    })
    assert item["title"] == "ACME LTD"
    assert item["subtitle"] == "5 Jan 2026 · Bank · +KES 10.00"
    assert item["account_nickname"] == "Bank"


def test_plan_item():
    pid = uuid4()
    item = plan_item({"id": pid, "name": "November", "period": "monthly", "status": "archived",
                      "starts_at": datetime(2026, 11, 1, tzinfo=timezone.utc)})
    assert item == {"type": "plan", "id": str(pid), "title": "November", "subtitle": "Monthly · Nov 2026 · Archived",
                    "link": {"plan_id": str(pid)}}


def test_plan_month_is_the_local_month():
    # Stored as local (EAT) midnight: 1 Nov 00:00 +03 is 31 Oct 21:00 UTC.
    item = plan_item({"id": uuid4(), "name": "x", "period": "monthly", "status": "draft",
                      "starts_at": datetime(2026, 10, 31, 21, tzinfo=timezone.utc)})
    assert item["subtitle"] == "Monthly · Nov 2026 · Draft"


def test_plan_item_without_name():
    item = plan_item({"id": uuid4(), "name": None, "period": "custom", "status": "draft",
                      "starts_at": datetime(2026, 2, 1, tzinfo=timezone.utc)})
    assert item["title"] == "Untitled plan"
    assert item["subtitle"] == "Custom · Feb 2026 · Draft"


def test_account_item():
    aid = uuid4()
    item = account_item({"id": aid, "nickname": "Daily", "provider": "M-Pesa", "account_type": "mobile_money",
                         "is_active": True})
    assert item == {"type": "account", "id": str(aid), "title": "Daily", "subtitle": "M-Pesa · Mobile money",
                    "link": {"account_id": str(aid)}}


def test_inactive_account_item_says_so():
    item = account_item({"id": uuid4(), "nickname": "Old", "provider": None, "account_type": "sacco",
                         "is_active": False})
    assert item["subtitle"] == "SACCO · Inactive"
