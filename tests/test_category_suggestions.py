"""ab-253: matching rows against the shared list and the user's own rules,
and building the preview's suggestions. Pure - no DB."""

from uuid import uuid4

import pytest

from app.services.category_suggestions import (
    Directory,
    build_preview_rows,
    extract_keys,
    pick_rule,
    pick_shared,
    rule_key,
    rule_lookup_pairs,
    vote_keys,
)
from app.services.merchant_keys import RowKey

SECRET = "test-secret"


def merchant(name, category, *, direction="out", patterns=(), numbers=()):
    """numbers: (kind, value) or (kind, value, requires_text)."""
    mid = uuid4()
    rows = [
        {"merchant_id": mid, "display_name": name, "category_name": category, "direction": direction,
         "key_kind": "name_pattern", "key_value": p, "requires_text": False}
        for p in patterns
    ]
    for n in numbers:
        kind, value, requires_text = (*n, False) if len(n) == 2 else n
        rows.append({"merchant_id": mid, "display_name": name, "category_name": category, "direction": direction,
                     "key_kind": kind, "key_value": value, "requires_text": requires_text})
    return rows


def name_key(text):
    return RowKey(kind="name", value=text, name=text, match_text=text)


def number_key(kind, number, name=""):
    return RowKey(kind=kind, value=number, number_kind=kind, number=number, name=name or None, match_text=name)


# --- Directory matching ---------------------------------------------------


def test_pattern_matches_whole_words_only():
    d = Directory.from_rows(merchant("Naivas", "Groceries", patterns=["NAIVAS"]), blocked=[])
    assert d.match(name_key("NAIVAS WESTLANDS"), "out").display_name == "Naivas"
    assert d.match(name_key("NAIVASHA BOARDING"), "out") is None


@pytest.mark.parametrize("text", ["UBER *EATS", "UBER EATS NAIROBI"])
def test_star_matches_a_literal_asterisk_or_nothing(text):
    d = Directory.from_rows(merchant("Uber Eats", "Dining Out", patterns=["UBER *EATS"]), blocked=[])
    assert d.match(name_key(text), "out") is not None


def test_star_spans_words_in_the_middle():
    d = Directory.from_rows(
        merchant("Google One", "Subscriptions", patterns=["MERCHANT ATM TRANSACTION * GOOGLE ONE"]), blocked=[]
    )
    assert d.match(name_key("MERCHANT ATM TRANSACTION GOOGLE ONE"), "out") is not None


def test_pattern_ending_in_punctuation_has_no_right_boundary():
    d = Directory.from_rows(merchant("KFC", "Dining Out", patterns=["KFC-"]), blocked=[])
    assert d.match(name_key("KFC-WESTGATE"), "out") is not None


def test_longest_pattern_wins():
    rows = merchant("Google", "Subscriptions", patterns=["GOOGLE *"]) + merchant(
        "Google One", "Subscriptions", patterns=["GOOGLE *GOOGLE ONE"]
    )
    d = Directory.from_rows(rows, blocked=[])
    assert d.match(name_key("GOOGLE *GOOGLE ONE"), "out").display_name == "Google One"


def test_number_beats_text():
    rows = merchant("KPLC", "Utilities", numbers=[("paybill", "888880")]) + merchant(
        "Other", "Household", patterns=["KPLC PREPAID"]
    )
    d = Directory.from_rows(rows, blocked=[])
    assert d.match(number_key("paybill", "888880", "KPLC PREPAID"), "out").display_name == "KPLC"


def test_number_kind_must_match():
    d = Directory.from_rows(merchant("KPLC", "Utilities", numbers=[("paybill", "888880")]), blocked=[])
    assert d.match(number_key("till", "888880"), "out") is None


def test_requires_text_number_needs_a_matching_name():
    rows = merchant("NTSA", "Taxes & government fees", patterns=["NTSA"], numbers=[("paybill", "206206", True)])
    d = Directory.from_rows(rows, blocked=[])
    assert d.match(number_key("paybill", "206206", "NTSA KENYA"), "out") is not None
    assert d.match(number_key("paybill", "206206", "SOMETHING ELSE"), "out") is None


def test_blocked_number_never_matches_alone():
    d = Directory.from_rows(
        merchant("X", "Household", numbers=[("paybill", "222222")]),
        blocked=[{"number_kind": "paybill", "number": "222222"}],
    )
    assert d.match(number_key("paybill", "222222"), "out") is None


def test_direction_is_respected():
    d = Directory.from_rows(merchant("Naivas", "Groceries", patterns=["NAIVAS"]), blocked=[])
    assert d.match(name_key("NAIVAS"), "in") is None


def test_rows_without_match_text_never_match_by_text():
    d = Directory.from_rows(merchant("KFC", "Dining Out", patterns=["KFC"]), blocked=[])
    assert d.match(RowKey(kind="reference", value="h:abc", provider="M-Pesa"), "out") is None


# --- picking one of the user's rules --------------------------------------


def rule(category_id, *, times_used=1, avg_amount=None, account_hash=None, label=None):
    return {"category_id": category_id, "times_used": times_used, "avg_amount": avg_amount,
            "account_hash": account_hash, "label": label}


def test_most_used_rule_wins():
    a, b = uuid4(), uuid4()
    assert pick_rule([rule(a, times_used=2), rule(b, times_used=5)], account_hash=None, amount=100)["category_id"] == b


def test_tie_breaks_on_closest_average_amount():
    a, b = uuid4(), uuid4()
    rules = [rule(a, times_used=3, avg_amount=500), rule(b, times_used=3, avg_amount=5000)]
    assert pick_rule(rules, account_hash=None, amount=4500)["category_id"] == b


def test_unbreakable_tie_gives_no_suggestion():
    rules = [rule(uuid4(), times_used=3), rule(uuid4(), times_used=3)]
    assert pick_rule(rules, account_hash=None, amount=100) is None


def test_same_account_reference_is_preferred():
    rent, fees = uuid4(), uuid4()
    rules = [rule(rent, times_used=9, account_hash="h:rent"), rule(fees, times_used=1, account_hash="h:fees")]
    assert pick_rule(rules, account_hash="h:fees", amount=100)["category_id"] == fees


def test_new_account_on_a_paybill_used_for_several_categories_gives_no_suggestion():
    rules = [rule(uuid4(), times_used=9, account_hash="h:rent"), rule(uuid4(), times_used=1, account_hash="h:fees")]
    assert pick_rule(rules, account_hash="h:new", amount=100) is None


def test_new_account_on_a_single_category_paybill_still_suggests():
    cat = uuid4()
    rules = [rule(cat, account_hash="h:a"), rule(cat, account_hash="h:b")]
    assert pick_rule(rules, account_hash="h:new", amount=100)["category_id"] == cat


def test_no_rules_gives_none():
    assert pick_rule([], account_hash=None, amount=100) is None


# --- building the preview -------------------------------------------------

GROCERIES = {"id": uuid4(), "name": "Groceries", "type": "expense"}
UTILITIES = {"id": uuid4(), "name": "Utilities", "type": "expense"}
SALARY = {"id": uuid4(), "name": "Salary", "type": "income"}
CATEGORIES = [GROCERIES, UTILITIES, SALARY]


def txn(description, *, amount=1000.0, direction="out", allocated=0.0, category_id=None, is_transfer=False):
    return {"id": uuid4(), "txn_date": "2026-10-01", "amount": amount, "currency": "KES", "direction": direction,
            "description": description, "allocated": allocated, "category_id": category_id, "is_transfer": is_transfer}


def preview(rows, *, provider="M-Pesa", rules=(), directory_rows=(), categories=CATEGORIES, shared_votes=None,
            blocked=()):
    keys = extract_keys(rows, provider=provider, secret=SECRET)
    return build_preview_rows(
        rows, keys=keys, rules=list(rules), directory=Directory.from_rows(list(directory_rows), blocked=list(blocked)),
        categories=categories, shared_votes=shared_votes or {},
    )


def stored_rule(category, *, kind, value, direction="out", provider=None, label=None, times_used=1):
    return {"key_kind": kind, "key_value": value, "provider": provider, "account_hash": None,
            "direction": direction, "category_id": category["id"], "label": label, "times_used": times_used,
            "avg_amount": None}


def test_directory_suggestion_maps_to_the_users_category_ignoring_case():
    rows = [txn("Merchant Payment to 5555555 - NAIVAS WESTLANDS")]
    out = preview(rows, directory_rows=merchant("Naivas", "groceries", patterns=["NAIVAS"]))
    s = out[0]["suggestion"]
    assert s["category_id"] == GROCERIES["id"]
    assert s["category_name"] == "Groceries"
    assert s["source"] == "directory"
    assert s["merchant_name"] == "Naivas"
    assert s["is_new_category"] is False


def test_directory_category_the_user_lacks_is_suggested_as_new():
    rows = [txn("Merchant Payment to 5555555 - SHELL RIRUTA")]
    out = preview(rows, directory_rows=merchant("Shell", "Fuel", patterns=["SHELL"]))
    s = out[0]["suggestion"]
    assert s["category_id"] is None
    assert s["category_name"] == "Fuel"
    assert s["is_new_category"] is True


def test_directory_category_of_the_wrong_type_is_not_reused():
    rows = [txn("Merchant Payment to 5555555 - NAIVAS")]
    cats = [{"id": uuid4(), "name": "Groceries", "type": "income"}]
    out = preview(rows, directory_rows=merchant("Naivas", "Groceries", patterns=["NAIVAS"]), categories=cats)
    assert out[0]["suggestion"]["is_new_category"] is True


def test_users_rule_beats_the_directory_and_carries_the_label():
    rows = [txn("Pay Bill Online to 888880 - KPLC PREPAID Acc. 0123")]
    out = preview(
        rows,
        rules=[stored_rule(GROCERIES, kind="paybill", value="888880", label="Home tokens")],
        directory_rows=merchant("KPLC", "Utilities", numbers=[("paybill", "888880")]),
    )
    assert out[0]["suggestion"]["source"] == "your_rule"
    assert out[0]["suggestion"]["category_id"] == GROCERIES["id"]
    assert out[0]["label"] == "Home tokens"
    assert out[0]["display_name"] == "Home tokens"


def test_rule_for_another_direction_is_ignored():
    rows = [txn("Pay Bill Online to 888880 - KPLC PREPAID", direction="out")]
    out = preview(rows, rules=[stored_rule(SALARY, kind="paybill", value="888880", direction="in")])
    assert out[0]["suggestion"] is None


def test_reference_rule_needs_the_same_provider():
    rows = [txn("IB Outgoing Mobile Payments PayBill FTX26232ABCDE 123456")]
    keys = extract_keys(rows, provider="NCBA Bank", secret=SECRET)
    value = keys[rows[0]["id"]].value
    match = stored_rule(UTILITIES, kind="reference", value=value, provider="NCBA Bank")
    other = stored_rule(UTILITIES, kind="reference", value=value, provider="Equity Bank")
    assert preview(rows, provider="NCBA Bank", rules=[match])[0]["suggestion"]["source"] == "your_rule"
    assert preview(rows, provider="NCBA Bank", rules=[other])[0]["suggestion"] is None


def test_category_already_chosen_for_the_same_key_in_this_statement():
    done = txn("Merchant Payment to 7700590 - PAZA BARBER SPA", allocated=1000.0, category_id=UTILITIES["id"])
    todo = txn("Merchant Payment to 7700590 - PAZA BARBER SPA")
    out = preview([done, todo])
    assert [r["transaction_id"] for r in out] == [todo["id"]]  # reconciled rows aren't listed
    assert out[0]["suggestion"]["source"] == "this_statement"
    assert out[0]["suggestion"]["category_id"] == UTILITIES["id"]


def test_rule_beats_this_statement():
    done = txn("Merchant Payment to 7700590 - PAZA BARBER SPA", allocated=1000.0, category_id=UTILITIES["id"])
    todo = txn("Merchant Payment to 7700590 - PAZA BARBER SPA")
    out = preview([done, todo], rules=[stored_rule(GROCERIES, kind="till", value="7700590")])
    assert out[0]["suggestion"]["source"] == "your_rule"


def test_partial_and_transfer_rows_are_left_out():
    rows = [txn("A SHOP LTD", allocated=10.0), txn("B SHOP LTD", is_transfer=True), txn("C SHOP LTD")]
    out = preview(rows, provider="Equity Bank")
    assert [r["description"] for r in out] == ["C SHOP LTD"]


def test_same_key_rows_share_an_opaque_group_key():
    rows = [
        txn("Customer Transfer to 254700000000 - JOHN KAMAU"),
        txn("Customer Transfer to 254700000000 - JOHN KAMAU"),
        txn("Customer Transfer to 254711111111 - MARY WANJIKU"),
    ]
    out = preview(rows)
    assert out[0]["group_key"] == out[1]["group_key"] != out[2]["group_key"]
    assert "JOHN" not in out[0]["group_key"] and "2547" not in out[0]["group_key"]


def test_rows_with_no_key_have_no_group_and_no_suggestion():
    out = preview([txn("OverDraft of Credit Party", direction="in")])
    assert out[0]["group_key"] is None
    assert out[0]["suggestion"] is None
    assert out[0]["key_kind"] is None


def test_display_name_falls_back_from_name_to_number_label_to_description():
    rows = [txn("IB Outgoing Mobile Payments BuyGoods FTX26216ABCDE 9120385")]
    assert preview(rows, provider="NCBA Bank")[0]["display_name"] == "Till 9120385"
    rows = [txn("Merchant Payment to 7700590 - PAZA BARBER SPA")]
    assert preview(rows)[0]["display_name"] == "PAZA BARBER SPA"


def test_preview_row_carries_number_and_business_flags_for_the_ui():
    out = preview([txn("Pay Bill Online to 888880 - KPLC PREPAID")])
    r = out[0]
    assert (r["key_kind"], r["number_kind"], r["number"], r["is_business"]) == ("paybill", "paybill", "888880", True)


def test_rule_lookup_pairs_are_unique_kind_value_pairs():
    rows = [txn("Pay Bill to 888880 - KPLC PREPAID"), txn("Pay Bill to 888880 - KPLC PREPAID"),
            txn("OverDraft of Credit Party", direction="in")]
    keys = extract_keys(rows, provider="M-Pesa", secret=SECRET)
    assert rule_lookup_pairs(keys.values(), Directory.from_rows([], blocked=[])) == [("paybill", "888880")]


def test_trailing_star_after_a_space_keeps_the_word_boundary():
    d = Directory.from_rows(merchant("Google", "Subscriptions", patterns=["GOOGLE *"]), blocked=[])
    assert d.match(name_key("GOOGLE *YOUTUBE"), "out") is not None
    assert d.match(name_key("GOOGLEPLEX CAFE"), "out") is None


# --- rule keys for blocked numbers (ab-253 PR 2) --------------------------

BLOCKED = [{"number_kind": "paybill", "number": "222222"}]


def test_rule_key_uses_the_number_normally():
    d = Directory.from_rows([], blocked=BLOCKED)
    assert rule_key(number_key("paybill", "888880", "KPLC PREPAID"), d) == ("paybill", "888880", None)


def test_rule_key_for_a_blocked_number_falls_back_to_the_name():
    d = Directory.from_rows([], blocked=BLOCKED)
    assert rule_key(number_key("paybill", "222222", "ECITIZEN"), d) == ("name", "ECITIZEN", None)
    assert rule_key(number_key("paybill", "222222"), d) is None


def test_rule_key_keeps_the_provider_for_references_only():
    d = Directory.from_rows([], blocked=[])
    ref = RowKey(kind="reference", value="h:abc", provider="NCBA Bank")
    assert rule_key(ref, d) == ("reference", "h:abc", "NCBA Bank")
    assert rule_key(RowKey(kind=None, value=None), d) is None


def test_preview_uses_the_name_rule_for_a_blocked_number():
    rows = [txn("Pay Bill Online to 222222 - ECITIZEN Acc. ABC")]
    keys = extract_keys(rows, provider="M-Pesa", secret=SECRET)
    d = Directory.from_rows([], blocked=BLOCKED)
    assert rule_lookup_pairs(keys.values(), d) == [("name", "ECITIZEN")]
    out = build_preview_rows(rows, keys=keys, directory=d, categories=CATEGORIES,
                             rules=[stored_rule(UTILITIES, kind="name", value="ECITIZEN")])
    assert out[0]["suggestion"]["source"] == "your_rule"




# --- others: what most users chose (ab-253 PR 3, no admin gate) -----------


def votes(*pairs):
    return [{"category_name": name, "users": n} for name, n in pairs]


def test_pick_shared_needs_three_voters_and_a_clear_winner():
    assert pick_shared(votes(("Groceries", 2))) is None                       # only 2 voters
    assert pick_shared(votes(("Groceries", 3))) == "Groceries"
    assert pick_shared(votes(("Groceries", 2), ("Household", 1))) == "Groceries"
    assert pick_shared(votes(("Groceries", 2), ("Household", 2))) is None     # tie
    assert pick_shared(votes(("A", 1), ("B", 1), ("C", 1))) is None           # nobody agrees
    assert pick_shared([]) is None


def test_vote_keys_follow_the_voting_rules():
    d = Directory.from_rows([], blocked=[{"number_kind": "paybill", "number": "222222"}])
    biz = RowKey(kind="till", value="5555555", number_kind="till", number="5555555", name="RUBIS ENERGY LTD",
                 match_text="RUBIS ENERGY LTD", is_business=True)
    assert vote_keys(biz, "out", d) == (("name", "RUBIS ENERGY LTD", "out"), ("till", "5555555", "out"))
    person = RowKey(kind="till", value="7286859", number_kind="till", number="7286859", name="PETER OMONDI",
                    match_text="PETER OMONDI")
    assert vote_keys(person, "out", d) == (None, None)
    nameless = RowKey(kind="till", value="9120385", number_kind="till", number="9120385")
    assert vote_keys(nameless, "out", d) == (None, ("till", "9120385", "out"))
    blocked = RowKey(kind="paybill", value="222222", number_kind="paybill", number="222222", name="ECITIZEN LTD",
                     match_text="ECITIZEN LTD", is_business=True)
    assert vote_keys(blocked, "out", d) == (("name", "ECITIZEN LTD", "out"), None)
    assert vote_keys(RowKey(kind="reference", value="h:x", provider="M-Pesa"), "out", d) == (None, None)


RUBIS = "Merchant Payment to 6123456 - RUBIS ENERGY LTD"
FUEL = {"id": uuid4(), "name": "Fuel", "type": "expense"}
TRANSPORT = {"id": uuid4(), "name": "Transport", "type": "expense"}


def test_others_override_the_starter_list():
    out = preview([txn(RUBIS)], categories=[FUEL, TRANSPORT],
                  directory_rows=merchant("Rubis", "Transport", patterns=["RUBIS"]),
                  shared_votes={("name", "RUBIS ENERGY LTD", "out"): votes(("fuel", 3), ("Transport", 1))})
    s = out[0]["suggestion"]
    assert (s["source"], s["category_id"], s["is_new_category"]) == ("others", FUEL["id"], False)


def test_too_few_voters_falls_back_to_the_starter_list():
    out = preview([txn(RUBIS)], categories=[FUEL, TRANSPORT],
                  directory_rows=merchant("Rubis", "Transport", patterns=["RUBIS"]),
                  shared_votes={("name", "RUBIS ENERGY LTD", "out"): votes(("Fuel", 2))})
    assert out[0]["suggestion"]["source"] == "directory"
    assert out[0]["suggestion"]["category_id"] == TRANSPORT["id"]


def test_number_votes_beat_name_votes():
    out = preview([txn(RUBIS)], categories=[FUEL, TRANSPORT], shared_votes={
        ("name", "RUBIS ENERGY LTD", "out"): votes(("Transport", 5)),
        ("till", "6123456", "out"): votes(("Fuel", 3)),
    })
    assert out[0]["suggestion"]["category_name"] == "Fuel"


def test_name_votes_used_when_the_number_has_too_few():
    out = preview([txn(RUBIS)], categories=[FUEL, TRANSPORT], shared_votes={
        ("name", "RUBIS ENERGY LTD", "out"): votes(("Transport", 3)),
        ("till", "6123456", "out"): votes(("Fuel", 2)),
    })
    assert out[0]["suggestion"]["category_name"] == "Transport"


def test_others_category_the_user_lacks_is_new():
    out = preview([txn(RUBIS)], categories=[GROCERIES],
                  shared_votes={("till", "6123456", "out"): votes(("Fuel", 4))})
    s = out[0]["suggestion"]
    assert (s["category_id"], s["category_name"], s["is_new_category"]) == (None, "Fuel", True)


def test_users_own_rule_and_this_statement_beat_others():
    shared = {("till", "6123456", "out"): votes(("Fuel", 9))}
    ruled = preview([txn(RUBIS)], categories=[FUEL, TRANSPORT], shared_votes=shared,
                    rules=[stored_rule(TRANSPORT, kind="till", value="6123456")])
    assert ruled[0]["suggestion"]["source"] == "your_rule"
    done = txn(RUBIS, allocated=1000.0, category_id=TRANSPORT["id"])
    out = preview([done, txn(RUBIS)], categories=[FUEL, TRANSPORT], shared_votes=shared)
    assert out[0]["suggestion"]["source"] == "this_statement"


def test_person_named_till_never_uses_others():
    out = preview([txn("Merchant Payment to 7286859 - PETER OMONDI")],
                  shared_votes={("till", "7286859", "out"): votes(("Groceries", 5))})
    assert out[0]["suggestion"] is None


# --- reference row with a Paybill the user typed (ab-253 item 4) ------------


def test_typed_paybill_on_the_list_shows_that_merchants_name():
    rows = [txn("IB Outgoing Mobile Payments PayBill FTX26232ABCDE 123456")]
    keys = extract_keys(rows, provider="NCBA Bank", secret=SECRET)
    rule = {**stored_rule(UTILITIES, kind="reference", value=keys[rows[0]["id"]].value, provider="NCBA Bank"),
            "user_entered_paybill": "888880"}
    out = preview(rows, provider="NCBA Bank", rules=[rule],
                  directory_rows=merchant("KPLC", "Utilities", numbers=[("paybill", "888880")]))
    assert out[0]["suggestion"]["source"] == "your_rule"
    assert out[0]["suggestion"]["merchant_name"] == "KPLC"
    assert out[0]["display_name"] == "KPLC"


def test_label_still_beats_the_typed_paybills_merchant_name():
    rows = [txn("IB Outgoing Mobile Payments PayBill FTX26232ABCDE 123456")]
    keys = extract_keys(rows, provider="NCBA Bank", secret=SECRET)
    rule = {**stored_rule(UTILITIES, kind="reference", value=keys[rows[0]["id"]].value, provider="NCBA Bank",
                          label="Home tokens"), "user_entered_paybill": "888880"}
    out = preview(rows, provider="NCBA Bank", rules=[rule],
                  directory_rows=merchant("KPLC", "Utilities", numbers=[("paybill", "888880")]))
    assert out[0]["display_name"] == "Home tokens"
