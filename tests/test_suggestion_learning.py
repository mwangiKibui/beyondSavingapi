"""ab-253 PR 2: what one confirm teaches - the user's rules, shared-list
votes and unknown-number votes. Pure, no DB."""

from uuid import uuid4

from app.services.category_suggestions import Directory
from app.services.merchant_keys import RowKey
from app.services.suggestion_learning import ConfirmedRow, plan_learning

FUEL, RENT = uuid4(), uuid4()
BLOCKED = [{"number_kind": "paybill", "number": "222222"}]


def directory(rows=(), blocked=BLOCKED):
    return Directory.from_rows(list(rows), blocked=blocked)


def known(name, category, *, patterns=(), numbers=()):
    mid = uuid4()
    rows = [{"merchant_id": mid, "display_name": name, "category_name": category, "direction": "out",
             "key_kind": "name_pattern", "key_value": p, "requires_text": False} for p in patterns]
    rows += [{"merchant_id": mid, "display_name": name, "category_name": category, "direction": "out",
              "key_kind": k, "key_value": v, "requires_text": False} for k, v in numbers]
    return rows


def business_till(number="5555555", name="RUBIS ENERGY LTD"):
    return RowKey(kind="till", value=number, number_kind="till", number=number, name=name, match_text=name,
                  is_business=True)


def row(key, *, category_id=FUEL, category_name="Fuel", amount=1000.0, direction="out", **kw):
    return ConfirmedRow(key=key, direction=direction, amount=amount, category_id=category_id,
                        category_name=category_name, **kw)


# --- rules ----------------------------------------------------------------


def test_rows_with_the_same_key_and_category_become_one_rule_with_count_and_total():
    learning = plan_learning([row(business_till(), amount=1000), row(business_till(), amount=3000)], directory())
    assert learning.rules == [{
        "key_kind": "till", "key_value": "5555555", "provider": None, "account_hash": None,
        "direction": "out", "category_id": FUEL, "times": 2, "total": 4000.0,
    }]


def test_same_key_two_categories_gives_two_rules():
    learning = plan_learning(
        [row(business_till()), row(business_till(), category_id=RENT, category_name="Rent")], directory()
    )
    assert sorted(r["category_id"] for r in learning.rules) == sorted([FUEL, RENT])


def test_account_hash_separates_rules_on_one_paybill():
    a = RowKey(kind="paybill", value="247247", number_kind="paybill", number="247247", account_hash="h:a")
    b = RowKey(kind="paybill", value="247247", number_kind="paybill", number="247247", account_hash="h:b")
    learning = plan_learning([row(a), row(b, category_id=RENT, category_name="Rent")], directory())
    assert {(r["account_hash"], r["category_id"]) for r in learning.rules} == {("h:a", FUEL), ("h:b", RENT)}


def test_blocked_number_learns_under_the_name():
    key = RowKey(kind="paybill", value="222222", number_kind="paybill", number="222222", name="ECITIZEN",
                 match_text="ECITIZEN", is_business=False)
    learning = plan_learning([row(key, category_name="Taxes & government fees")], directory())
    assert (learning.rules[0]["key_kind"], learning.rules[0]["key_value"]) == ("name", "ECITIZEN")
    assert learning.number_votes == []


def test_reference_rule_keeps_the_provider():
    key = RowKey(kind="reference", value="h:ref", provider="NCBA Bank")
    learning = plan_learning([row(key)], directory())
    assert learning.rules[0]["provider"] == "NCBA Bank"


def test_rows_with_no_key_teach_nothing():
    learning = plan_learning([row(RowKey(kind=None, value=None))], directory())
    assert (learning.rules, learning.merchant_votes, learning.number_votes, learning.labels) == ([], [], [], [])


def test_label_and_entered_paybill_are_kept_per_key():
    key = RowKey(kind="reference", value="h:ref", provider="NCBA Bank")
    learning = plan_learning([row(key, label="Landlord", user_entered_paybill="247247")], directory())
    assert learning.labels == [{"key_kind": "reference", "key_value": "h:ref", "provider": "NCBA Bank",
                                "direction": "out", "label": "Landlord", "user_entered_paybill": "247247"}]


# --- shared-list votes ----------------------------------------------------


def test_unmatched_business_name_votes():
    learning = plan_learning([row(business_till())], directory())
    assert learning.merchant_votes == [{"merchant_key": "RUBIS ENERGY LTD", "direction": "out", "category_name": "Fuel"}]


def test_name_already_on_the_approved_list_does_not_vote():
    learning = plan_learning([row(business_till())], directory(known("Rubis", "Transport", patterns=["RUBIS"])))
    assert learning.merchant_votes == []


def test_person_named_till_never_votes():
    key = RowKey(kind="till", value="7286859", number_kind="till", number="7286859", name="PETER OMONDI",
                 match_text="PETER OMONDI", is_business=False)
    learning = plan_learning([row(key)], directory())
    assert learning.merchant_votes == [] and learning.number_votes == []


def test_reference_rows_never_vote():
    learning = plan_learning([row(RowKey(kind="reference", value="h:x", provider="M-Pesa"))], directory())
    assert learning.merchant_votes == [] and learning.number_votes == []


def test_one_vote_per_name_uses_the_users_most_common_category():
    rows = [row(business_till()), row(business_till()),
            row(business_till(), category_id=RENT, category_name="Rent")]
    learning = plan_learning(rows, directory())
    assert [v["category_name"] for v in learning.merchant_votes] == ["Fuel"]


# --- unknown-number votes -------------------------------------------------


def test_unknown_business_number_votes():
    learning = plan_learning([row(business_till())], directory())
    assert learning.number_votes == [{"number_kind": "till", "number": "5555555", "direction": "out",
                                      "category_name": "Fuel"}]


def test_nameless_number_votes():
    key = RowKey(kind="till", value="9120385", number_kind="till", number="9120385")
    assert len(plan_learning([row(key)], directory()).number_votes) == 1


def test_number_already_on_the_list_does_not_vote():
    d = directory(known("Rubis", "Transport", numbers=[("till", "5555555")]))
    assert plan_learning([row(business_till())], d).number_votes == []
