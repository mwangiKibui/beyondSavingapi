"""ab-285: Funds transfer suggestions - the user's own transfer rule,
transfer wording (income keywords win), and money in sent by the user."""

from uuid import uuid4

import pytest

from app.services.category_suggestions import Directory, build_preview_rows, extract_keys
from app.services.description_keywords import KeywordContext, KeywordList
from app.services.payee_groups import group_suggestion

SECRET = "test-secret"
SALARY = {"id": uuid4(), "name": "Salary", "type": "income"}
CATEGORIES = [SALARY]
BETWEEN = {"id": uuid4(), "name": "Between my accounts", "direction": None}
FAMILY_IN = {"id": uuid4(), "name": "From family", "direction": "in"}
SAVINGS_OUT = {"id": uuid4(), "name": "Savings Contribution", "direction": "out"}
REASONS = [BETWEEN, FAMILY_IN, SAVINGS_OUT]


def kw(pattern, name, direction="in", target="category"):
    return {"id": uuid4(), "pattern": pattern, "direction": direction, "category_name": name, "target": target}


KEYWORDS = KeywordList.from_rows([
    kw("SALARY", "Salary"),
    *(kw(p, "Between my accounts", d, "transfer") for p in ("PESALINK", "RTGS", "FUNDS TRANSFER")
      for d in ("in", "out")),
])
OWNER = {"first_name": "Wanjiru", "last_name": "Kamau", "account_numbers": ["0123456789"]}


def txn(description, *, direction="in", amount=5000.0):
    return {"id": uuid4(), "txn_date": "2026-11-01", "amount": amount, "currency": "KES", "direction": direction,
            "description": description, "allocated": 0.0, "category_id": None, "is_transfer": False}


def suggest(row, *, rules=(), reasons=REASONS, owner=None, provider="Equity Bank"):
    keys = extract_keys([row], provider=provider, secret=SECRET)
    out = build_preview_rows(
        [row], keys=keys, rules=list(rules), directory=Directory.from_rows([], blocked=[]), categories=CATEGORIES,
        keywords=KeywordContext(keywords=KEYWORDS), reasons=reasons, owner=owner,
    )
    return out[0]["suggestion"]


# --- transfer wording ---------------------------------------------------------


@pytest.mark.parametrize("text, direction", [
    ("PESALINK TO JOHN SMITH", "out"), ("RTGS FROM ACME LTD", "in"), ("FUNDS TRANSFER REF 99", "out"),
])
def test_transfer_wording_suggests_a_funds_transfer(text, direction):
    s = suggest(txn(text, direction=direction))
    assert s["kind"] == "transfer" and s["source"] == "keyword"
    assert (s["transfer_reason_id"], s["category_name"]) == (BETWEEN["id"], "Between my accounts")
    assert s["category_id"] is None and s["keyword"] in ("pesalink", "rtgs", "funds transfer")


def test_income_wording_wins_over_transfer_wording():
    s = suggest(txn("RTGS ACME LTD SALARY NOV"))
    assert (s["category_name"], s.get("kind", "category")) == ("Salary", "category")


def test_a_reason_the_user_lacks_is_named_for_creation_on_approval():
    s = suggest(txn("PESALINK FROM ACME"), reasons=[FAMILY_IN])
    assert s["kind"] == "transfer" and s["transfer_reason_id"] is None
    assert s["category_name"] == "Between my accounts"


# --- the user's own transfer rule ------------------------------------------------


def test_a_remembered_transfer_is_suggested_as_your_rule():
    r = txn("Funds received from 0700000000 - JANE DOE")
    key = extract_keys([r], provider="M-Pesa", secret=SECRET)[r["id"]]
    from app.services.category_suggestions import rule_key
    kind, value, provider = rule_key(key, Directory.from_rows([], blocked=[]))
    rule = {"key_kind": kind, "key_value": value, "provider": provider, "account_hash": key.account_hash,
            "direction": "in", "category_id": None, "transfer_reason_id": FAMILY_IN["id"], "label": None,
            "user_entered_paybill": None, "times_used": 2, "avg_amount": None}

    s = suggest(r, rules=[rule], provider="M-Pesa")
    assert (s["kind"], s["source"], s["transfer_reason_id"], s["category_name"]) == (
        "transfer", "your_rule", FAMILY_IN["id"], "From family")


def test_a_remembered_reason_for_the_other_direction_is_ignored():
    r = txn("Funds received from 0700000000 - JANE DOE")
    key = extract_keys([r], provider="M-Pesa", secret=SECRET)[r["id"]]
    from app.services.category_suggestions import rule_key
    kind, value, provider = rule_key(key, Directory.from_rows([], blocked=[]))
    rule = {"key_kind": kind, "key_value": value, "provider": provider, "account_hash": key.account_hash,
            "direction": "in", "category_id": None, "transfer_reason_id": SAVINGS_OUT["id"], "label": None,
            "user_entered_paybill": None, "times_used": 2, "avg_amount": None}
    assert suggest(r, rules=[rule], provider="M-Pesa") is None


# --- money in from the user themselves ---------------------------------------------


@pytest.mark.parametrize("text", [
    "Funds received from 0700000000 - WANJIRU KAMAU",
    "IFT FROM ACC 0123456789",
    "deposit kamau wanjiru",
])
def test_money_in_from_the_user_is_suggested_as_own_account(text):
    s = suggest(txn(text), owner=OWNER, provider="M-Pesa")
    assert (s["kind"], s["source"], s["transfer_reason_id"]) == ("transfer", "own_account", BETWEEN["id"])


@pytest.mark.parametrize("text, direction", [
    ("Funds received from 0700000000 - WANJIRU NJERI", "in"),  # first name only
    ("Customer Transfer to 0700000000 - WANJIRU KAMAU", "out"),  # money out
    ("FROM 01234567", "in"),  # partial number
    ("WANJIRUKAMAU LTD", "in"),  # not whole words
])
def test_own_account_needs_both_names_or_a_full_number_on_money_in(text, direction):
    s = suggest(txn(text, direction=direction), owner=OWNER, provider="M-Pesa")
    assert s is None or s["source"] != "own_account"


# --- grouping ---------------------------------------------------------------------


def test_a_transfer_never_merges_with_a_category_of_the_same_name():
    cat = {"category_id": uuid4(), "category_name": "Between my accounts", "is_new_category": False,
           "source": "your_rule"}
    transfer = {"kind": "transfer", "transfer_reason_id": BETWEEN["id"], "category_id": None,
                "category_name": "Between my accounts", "is_new_category": False, "source": "keyword"}
    assert group_suggestion([cat, transfer]) is None
    assert group_suggestion([transfer, transfer, cat]) == transfer


def test_a_group_reports_its_transfer_reason_when_every_row_agrees():
    from app.services.payee_groups import build_groups

    rows = [dict(txn("PESALINK FROM ACME"), transfer_reason_id=BETWEEN["id"],
                 transfer_reason_name="Between my accounts") for _ in range(2)]
    keys = extract_keys(rows, provider="Equity Bank", secret=SECRET)
    [group] = build_groups(rows, keys=keys)
    assert (group["transfer_reason_id"], group["transfer_reason_name"]) == (BETWEEN["id"], "Between my accounts")
    assert group["rows"][0]["transfer_reason_name"] == "Between my accounts"

    rows[1]["transfer_reason_id"], rows[1]["transfer_reason_name"] = FAMILY_IN["id"], "From family"
    [group] = build_groups(rows, keys=keys)
    assert group["transfer_reason_id"] is None
