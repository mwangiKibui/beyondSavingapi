"""ab-290: category suggestions from description keywords."""

from uuid import uuid4

import pytest

from app.services.category_suggestions import Directory, build_preview_rows, extract_keys
from app.services.description_keywords import KeywordContext, KeywordList, pick_shared_keyword

SECRET = "test-secret"
GROCERIES = {"id": uuid4(), "name": "Groceries", "type": "expense"}
SALARY = {"id": uuid4(), "name": "Salary", "type": "income"}
CONSULTING = {"id": uuid4(), "name": "Consulting", "type": "income"}
DIVIDENDS = {"id": uuid4(), "name": "SACCO dividends & interest", "type": "income"}
CATEGORIES = [GROCERIES, SALARY, CONSULTING, DIVIDENDS]


def kw(pattern, category_name, direction="in"):
    return {"id": uuid4(), "pattern": pattern, "direction": direction, "category_name": category_name}


SALARY_KW = kw("SALARY", "Salary")
NET_PAY_KW = kw("NET PAY", "Salary")
RENT_IN_KW = kw("RENT", "Rental Income")
RENT_OUT_KW = kw("RENT", "Rent", "out")
INSURANCE_KW = kw("INSURANCE", "Insurance", "out")
MEDICAL_INSURANCE_KW = kw("MEDICAL INSURANCE", "Medical", "out")
DIVIDEND_KW = kw("DIVIDEND", "SACCO dividends & interest")
INTEREST_KW = kw("INTEREST ON DEPOSITS", "SACCO dividends & interest")
STARTER = KeywordList.from_rows(
    [SALARY_KW, NET_PAY_KW, RENT_IN_KW, RENT_OUT_KW, INSURANCE_KW, MEDICAL_INSURANCE_KW, DIVIDEND_KW, INTEREST_KW]
)


def txn(description, *, direction="in", amount=50000.0):
    return {"id": uuid4(), "txn_date": "2026-11-01", "amount": amount, "currency": "KES", "direction": direction,
            "description": description, "allocated": 0.0, "category_id": None, "is_transfer": False}


def preview(rows, *, ctx=None, categories=CATEGORIES, provider="Equity Bank", directory_rows=(), rules=()):
    keys = extract_keys(rows, provider=provider, secret=SECRET)
    return build_preview_rows(
        rows, keys=keys, rules=list(rules), directory=Directory.from_rows(list(directory_rows), blocked=[]),
        categories=categories, keywords=ctx if ctx is not None else KeywordContext(keywords=STARTER),
    )


# --- matching ---------------------------------------------------------------


@pytest.mark.parametrize("text", ["SALARY NOV 2026", "Nov salary - ACME", "EFT  SALARY/ADV"])
def test_matches_a_whole_word_ignoring_case_and_spacing(text):
    assert STARTER.match(text, "in").id == SALARY_KW["id"]


@pytest.mark.parametrize("text", ["SALARYMAN LTD", "PRESALARY", "RENTAL CAR HIRE", "PARENTS"])
def test_never_matches_inside_another_word(text):
    assert STARTER.match(text, "in") is None and STARTER.match(text, "out") is None


def test_only_matches_its_direction():
    assert STARTER.match("SALARY NOV", "out") is None
    assert STARTER.match("RENT NOV", "in").category_name == "Rental Income"
    assert STARTER.match("RENT NOV", "out").category_name == "Rent"


def test_the_longest_keyword_wins():
    assert STARTER.match("JUBILEE MEDICAL INSURANCE", "out").id == MEDICAL_INSURANCE_KW["id"]


def test_matched_ids_lists_each_keyword_once():
    rows = [txn("SALARY OCT"), txn("SALARY NOV"), txn("SHOPPING", direction="out")]
    assert STARTER.matched_ids(rows) == [SALARY_KW["id"]]


def test_no_description_matches_nothing():
    assert STARTER.match(None, "in") is None and STARTER.match("   ", "in") is None


@pytest.mark.parametrize("votes, expected", [
    ([], None),
    ([{"category_name": "Consulting", "users": 2}], None),
    ([{"category_name": "Consulting", "users": 3}], "Consulting"),
    ([{"category_name": "Consulting", "users": 3}, {"category_name": "Bonus", "users": 3}], None),
    ([{"category_name": "Bonus", "users": 1}, {"category_name": "Consulting", "users": 4}], "Consulting"),
])
def test_shared_pick_needs_three_users_and_a_clear_lead(votes, expected):
    assert pick_shared_keyword(votes) == expected


# --- in the suggestion pipeline ----------------------------------------------


def test_salary_is_suggested_from_the_description():
    out = preview([txn("SALARY NOV 2026")])
    s = out[0]["suggestion"]
    assert (s["category_id"], s["source"], s["keyword"]) == (SALARY["id"], "keyword", "salary")
    assert out[0]["keyword_match"] == {"id": SALARY_KW["id"], "category_name": "Salary", "target": "category"}


def test_a_new_category_is_offered_when_the_user_has_none():
    s = preview([txn("SALARY NOV 2026")], categories=[GROCERIES])[0]["suggestion"]
    assert s == {"category_id": None, "category_name": "Salary", "is_new_category": True, "source": "keyword",
                 "merchant_name": None, "keyword": "salary"}


def test_no_suggestion_when_the_direction_doesnt_match():
    out = preview([txn("SALARY ADVANCE REPAYMENT", direction="out")])
    assert out[0]["suggestion"] is None and out[0]["keyword_match"] is None


def test_sacco_dividends_and_interest_are_suggested_as_income():
    rows = [
        txn("SJN-20001 Dividend 2025", amount=4200.0),
        txn("SJN-20002 Interest on Deposits 2025", amount=1300.0),
        txn("SJN-20003 Dividend reversal", direction="out", amount=100.0),
    ]
    by_text = {r["description"]: r["suggestion"] for r in preview(rows, provider="Mentor Sacco")}
    assert by_text["SJN-20001 Dividend 2025"]["category_id"] == DIVIDENDS["id"]
    assert by_text["SJN-20002 Interest on Deposits 2025"]["source"] == "keyword"
    assert by_text["SJN-20003 Dividend reversal"] is None


def test_the_merchant_list_wins_over_a_keyword():
    rows = [txn("Merchant Payment to 5555555 - RENT A CAR LTD", direction="out", amount=3000.0)]
    directory = [{"merchant_id": uuid4(), "display_name": "Rent A Car", "category_name": "Groceries",
                  "direction": "out", "key_kind": "name_pattern", "key_value": "RENT A CAR", "requires_text": False}]
    s = preview(rows, provider="M-Pesa", directory_rows=directory)[0]["suggestion"]
    assert s["source"] == "directory" and s.get("keyword") is None


def test_your_rule_wins_over_a_keyword():
    rows = [txn("Funds received from - ACME LTD SALARY", amount=80000.0)]
    keys = extract_keys(rows, provider="M-Pesa", secret=SECRET)
    k = keys[rows[0]["id"]]
    rule = {"key_kind": k.kind, "key_value": k.value, "provider": k.provider, "account_hash": None, "direction": "in",
            "category_id": CONSULTING["id"], "label": None, "times_used": 1, "avg_amount": None}
    s = preview(rows, provider="M-Pesa", rules=[rule])[0]["suggestion"]
    assert (s["category_id"], s["source"]) == (CONSULTING["id"], "your_rule")


def test_the_users_own_choice_for_a_keyword_comes_first():
    ctx = KeywordContext(keywords=STARTER, choices={SALARY_KW["id"]: CONSULTING["id"]},
                         shared={SALARY_KW["id"]: [{"category_name": "Bonus", "users": 5}]})
    s = preview([txn("SALARY NOV 2026")], ctx=ctx)[0]["suggestion"]
    assert (s["category_id"], s["source"], s["keyword"]) == (CONSULTING["id"], "your_rule", "salary")


def test_a_choice_of_the_wrong_type_is_ignored():
    ctx = KeywordContext(keywords=STARTER, choices={SALARY_KW["id"]: GROCERIES["id"]})
    assert preview([txn("SALARY NOV 2026")], ctx=ctx)[0]["suggestion"]["category_id"] == SALARY["id"]


def test_three_users_agreeing_replaces_the_lists_category_for_everyone():
    ctx = KeywordContext(keywords=STARTER, shared={SALARY_KW["id"]: [{"category_name": "consulting", "users": 3}]})
    s = preview([txn("SALARY NOV 2026")], ctx=ctx)[0]["suggestion"]
    assert (s["category_id"], s["source"], s["keyword"]) == (CONSULTING["id"], "others", "salary")


def test_without_keywords_nothing_is_suggested():
    assert preview([txn("SALARY NOV 2026")], ctx=KeywordContext())[0]["suggestion"] is None
