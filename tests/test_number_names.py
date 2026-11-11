"""ab-253 PR 4: shared Till/Paybill -> name learning. Pure - no DB."""

from datetime import date, datetime, timezone
from uuid import uuid4

from app.services.category_suggestions import (
    Directory,
    apply_learned_names,
    build_preview_rows,
    extract_keys,
    learned_name_lookups,
    name_sightings,
    pick_learned_name,
    plan_key_growth,
    vote_keys,
)
from tests.test_category_suggestions import merchant

SECRET = "test-secret"
EMPTY = Directory.from_rows([], blocked=[])
T1 = datetime(2026, 11, 1, tzinfo=timezone.utc)
T2 = datetime(2026, 11, 5, tzinfo=timezone.utc)


def txn(description, *, direction="out", day=1):
    return {"id": uuid4(), "description": description, "direction": direction, "amount": 500.0,
            "txn_date": date(2026, 10, day), "currency": "KES", "allocated": 0, "category_id": None,
            "is_transfer": False, "balance_after": None}


def keyed(rows, provider):
    return extract_keys(rows, provider=provider, secret=SECRET)


def ncba_till(number="5551234"):
    return txn(f"IB Outgoing Mobile Payments BuyGoods FTX26232ABCDE {number}")


# --- sightings (what a confirm records) -------------------------------------


def test_mpesa_business_rows_are_sighted_with_their_latest_name():
    rows = [txn("Merchant Payment to 5551234 - NAIVAS WESTLANDS", day=1),
            txn("Merchant Payment to 5551234 - NAIVAS WESTLANDS LTD", day=9),
            txn("Pay Bill to 888880 - KPLC PREPAID Acc. 12345678901")]
    got = name_sightings(rows, keyed(rows, "M-Pesa"), EMPTY)
    assert sorted(got, key=lambda s: s["number"]) == [
        {"number_kind": "till", "number": "5551234", "direction": "out", "name": "NAIVAS WESTLANDS LTD"},
        {"number_kind": "paybill", "number": "888880", "direction": "out", "name": "KPLC PREPAID"},
    ]


def test_a_name_on_the_list_counts_as_a_business_even_without_ltd():
    rows = [txn("Merchant Payment to 5551234 - NAIVAS WESTLANDS"), txn("Merchant Payment to 5558888 - MWANGI SHOP")]
    rows.append(txn("Merchant Payment to 5559999 - JOHN KAMAU"))
    got = name_sightings(rows, keyed(rows, "M-Pesa"), naivas())
    assert sorted(s["name"] for s in got) == ["MWANGI SHOP", "NAIVAS WESTLANDS"]
    assert name_sightings(rows[:1], keyed(rows[:1], "M-Pesa"), EMPTY) == []


def test_person_named_tills_blocked_numbers_and_nameless_rows_are_never_sighted():
    rows = [txn("Merchant Payment to 5559999 - PETER OMONDI"),
            txn("Pay Bill Online to 222222 - ECITIZEN SERVICES LTD Acc. ABC123"),
            ncba_till(),
            txn("Customer Transfer to 0712XXX345 - JANE DOE")]
    blocked = Directory.from_rows([], blocked=[{"number_kind": "paybill", "number": "222222"}])
    assert name_sightings(rows[:2] + rows[3:], keyed(rows[:2] + rows[3:], "M-Pesa"), blocked) == []
    assert name_sightings([rows[2]], keyed([rows[2]], "NCBA Bank"), blocked) == []


def test_a_learned_name_is_never_sighted_again():
    row = ncba_till()
    keys = apply_learned_names([row], keyed([row], "NCBA Bank"), {("till", "5551234", "out"): [
        {"name": "NAIVAS WESTLANDS", "users": 1, "last_seen": T1}]}, EMPTY)
    assert name_sightings([row], keys, EMPTY) == []


# --- reading names for nameless rows ------------------------------------------


def test_only_nameless_unlisted_unblocked_numbers_are_looked_up():
    rows = [ncba_till("5551234"), ncba_till("5552222"), ncba_till("5553333"),
            txn("IB Outgoing Mobile Payments PayBill FTX26232ABCDE 123456")]
    d = Directory.from_rows(merchant("Rubis", "Fuel", numbers=[("till", "5552222")]),
                            blocked=[{"number_kind": "till", "number": "5553333"}])
    assert learned_name_lookups(rows, keyed(rows, "NCBA Bank"), d) == [("till", "5551234", "out")]


def test_pick_learned_name_most_users_then_most_recent():
    assert pick_learned_name([{"name": "A LTD", "users": 1, "last_seen": T2},
                              {"name": "B LTD", "users": 2, "last_seen": T1}])["name"] == "B LTD"
    assert pick_learned_name([{"name": "A LTD", "users": 1, "last_seen": T1},
                              {"name": "B LTD", "users": 1, "last_seen": T2}])["name"] == "B LTD"
    assert pick_learned_name([]) is None


def test_learned_name_fills_the_key_but_not_its_rule_identity():
    row = ncba_till()
    keys = keyed([row], "NCBA Bank")
    learned = apply_learned_names([row], keys, {("till", "5551234", "out"): [
        {"name": "NAIVAS WESTLANDS", "users": 1, "last_seen": T1}]}, EMPTY)
    k = learned[row["id"]]
    assert (k.name, k.match_text, k.is_business, k.name_learned) == (
        "NAIVAS WESTLANDS", "NAIVAS WESTLANDS", True, True)
    assert (k.kind, k.value, k.number) == ("till", "5551234", "5551234")
    assert keys[row["id"]].name is None  # input untouched


def test_learned_name_matches_the_list_and_reads_name_votes():
    row = ncba_till()
    d = Directory.from_rows(merchant("Naivas", "Groceries", patterns=["NAIVAS"]), blocked=[])
    keys = apply_learned_names([row], keyed([row], "NCBA Bank"), {("till", "5551234", "out"): [
        {"name": "NAIVAS WESTLANDS", "users": 1, "last_seen": T1}]}, d)
    assert vote_keys(keys[row["id"]], "out", d)[0] == ("name", "NAIVAS WESTLANDS", "out")
    out = build_preview_rows([row], keys=keys, rules=[], directory=d, categories=[])
    assert out[0]["display_name"] == "NAIVAS WESTLANDS" and out[0]["name_learned"] is True
    assert out[0]["suggestion"]["source"] == "directory"
    assert out[0]["suggestion"]["category_name"] == "Groceries"


def test_rows_without_a_learned_name_are_unchanged():
    row = ncba_till()
    keys = keyed([row], "NCBA Bank")
    out = build_preview_rows([row], keys=apply_learned_names([row], keys, {}, EMPTY), rules=[], directory=EMPTY,
                             categories=[])
    assert out[0]["display_name"] == "Till 5551234" and out[0]["name_learned"] is False


# --- growing the list ------------------------------------------------------------


def naivas():
    return Directory.from_rows(merchant("Naivas", "Groceries", patterns=["NAIVAS"]), blocked=[])


def test_three_users_seeing_a_listed_name_add_the_number_to_that_merchant():
    d = naivas()
    mid = next(iter(d._merchants))
    plan = plan_key_growth({("till", "5551234", "out"): [{"name": "NAIVAS WESTLANDS", "users": 3, "last_seen": T1}]}, d)
    assert plan == [{"merchant_id": mid, "direction": "out", "key_kind": "till", "key_value": "5551234"}]


def test_no_growth_below_three_users_or_for_unlisted_names():
    d = naivas()
    assert plan_key_growth({("till", "5551234", "out"): [{"name": "NAIVAS WESTLANDS", "users": 2, "last_seen": T1}]}, d) == []
    assert plan_key_growth({("till", "5551234", "out"): [{"name": "MAMA PIMA HARDWARE", "users": 5, "last_seen": T1}]}, d) == []


def test_growth_uses_the_top_name_only():
    d = naivas()
    learned = {("till", "5551234", "out"): [{"name": "NAIVAS WESTLANDS", "users": 3, "last_seen": T1},
                                            {"name": "QUICKMART KILIMANI", "users": 4, "last_seen": T1}]}
    assert plan_key_growth(learned, d) == []


def test_no_growth_for_owned_or_blocked_numbers_or_the_wrong_direction():
    rows = merchant("Naivas", "Groceries", patterns=["NAIVAS"]) + merchant("Rubis", "Fuel", numbers=[("till", "5551234")])
    d = Directory.from_rows(rows, blocked=[{"number_kind": "till", "number": "5559999"}])
    one = [{"name": "NAIVAS WESTLANDS", "users": 3, "last_seen": T1}]
    assert plan_key_growth({("till", "5551234", "out"): one, ("till", "5559999", "out"): one,
                            ("till", "5557777", "in"): one}, d) == []


def test_a_nameless_number_the_list_owns_shows_the_merchant_name_whatever_the_suggestion():
    row = ncba_till()
    d = Directory.from_rows(merchant("Naivas", "Groceries", numbers=[("till", "5551234")]), blocked=[])
    cat = {"id": uuid4(), "name": "Treats", "type": "expense"}
    rule = {"key_kind": "till", "key_value": "5551234", "provider": None, "direction": "out", "account_hash": None,
            "category_id": cat["id"], "times_used": 2, "avg_amount": 500.0, "label": None, "user_entered_paybill": None}
    out = build_preview_rows([row], keys=keyed([row], "NCBA Bank"), rules=[rule], directory=d, categories=[cat])
    assert out[0]["suggestion"]["source"] == "your_rule"
    assert out[0]["display_name"] == "Naivas" and out[0]["name_learned"] is False


def test_person_rows_are_never_sighted_ab269():
    rows = [txn("Customer Transfer Fuliza MPesa to - 254700***000 JOHN DOE"),
            txn("Funds received from - 254711***111 JANE ROE", direction="in")]
    assert name_sightings(rows, keyed(rows, "M-Pesa"), EMPTY) == []
