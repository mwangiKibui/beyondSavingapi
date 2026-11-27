"""ab-282: grouping rows by payee, across statements, plus filters/sort/pages."""

from datetime import date
from uuid import uuid4

import pytest

from app.services.category_suggestions import _is_open
from app.services.payee_groups import (
    build_groups,
    count_payees,
    extract_row_keys,
    group_suggestion,
    payee_group_key,
    review_tab,
    select_groups,
)

SECRET = "test-secret"
SEPT, OCT = uuid4(), uuid4()


def row(description, *, provider="M-Pesa", amount=100.0, direction="out", txn_date=date(2026, 10, 1),
        import_id=OCT, file_name="oct.pdf", **extra):
    return {"id": uuid4(), "txn_date": txn_date, "amount": amount, "currency": "KES", "direction": direction,
            "description": description, "provider": provider, "import_id": import_id, "file_name": file_name,
            "account_nickname": provider, "allocated": 0, "category_id": None, "is_transfer": False,
            "handled_as": None, **extra}


def groups_for(rows, **kwargs):
    keys = extract_row_keys(rows, secret=SECRET)
    return build_groups(rows, keys=keys, **kwargs)


GROCER = "Merchant Payment to 5555555 - ACME SUPERMARKET"
PERSON = "Customer Transfer to 0700000000 - JANE DOE"


def test_rows_from_two_statements_share_one_group():
    rows = [
        row(GROCER, amount=150, txn_date=date(2026, 9, 3), import_id=SEPT, file_name="sep.pdf"),
        row(GROCER, amount=250, txn_date=date(2026, 10, 9)),
        row("Pay Bill Online to 888880 - KPLC PREPAID Acc. 01234567890", amount=500),
    ]
    groups = groups_for(rows)

    assert len(groups) == 2
    grocer = next(g for g in groups if g["row_count"] == 2)
    assert grocer["total"] == 400
    assert grocer["display_name"] == "ACME SUPERMARKET"
    assert grocer["key_kind"] == "till" and grocer["is_person"] is False and grocer["direction"] == "out"
    assert [s["file_name"] for s in grocer["statements"]] == ["sep.pdf", "oct.pdf"]
    assert [r["txn_date"] for r in grocer["rows"]] == [date(2026, 9, 3), date(2026, 10, 9)]
    assert grocer["first_date"] == date(2026, 9, 3) and grocer["last_date"] == date(2026, 10, 9)


def test_keys_follow_each_rows_own_provider():
    # M-Pesa reads a Till from this text; another provider's statement doesn't.
    rows = [row(GROCER), row(GROCER, provider="NCBA Bank")]
    keys = extract_row_keys(rows, secret=SECRET)
    assert keys[rows[0]["id"]].kind == "till" and keys[rows[1]["id"]].kind != "till"
    assert len(build_groups(rows, keys=keys)) == 2


def test_directions_never_share_a_group():
    groups = groups_for([row(GROCER), row(GROCER, direction="in")])
    assert len(groups) == 2


def test_rows_without_a_payee_are_groups_of_one():
    rows = [row(""), row(None)]
    groups = groups_for(rows)
    assert len(groups) == 2
    assert all(g["group_key"].startswith("t:") and g["row_count"] == 1 for g in groups)


def test_group_key_hides_names_and_numbers():
    groups = groups_for([row(PERSON)])
    assert "JANE" not in groups[0]["group_key"] and "0700000000" not in groups[0]["group_key"]


def test_person_group_shows_statement_text_and_is_marked_person():
    groups = groups_for([row(PERSON)])
    assert groups[0]["is_person"] is True
    assert "JANE DOE" in groups[0]["display_name"]


def test_display_name_comes_from_the_latest_row():
    rows = [row("Merchant Payment to 5555555 - ACME SUPERMKT", txn_date=date(2026, 9, 1)),
            row(GROCER, txn_date=date(2026, 10, 1))]
    groups = groups_for(rows)
    assert len(groups) == 1 and groups[0]["display_name"] == "ACME SUPERMARKET"


def test_payee_group_key_falls_back_to_the_row():
    txn_id = uuid4()
    keys = extract_row_keys([row("", id=txn_id)], secret=SECRET)
    assert payee_group_key(keys[txn_id], "out", txn_id) == f"t:{txn_id}"


def test_category_and_apply_id_only_when_every_row_agrees():
    cat, other, apply_id = uuid4(), uuid4(), uuid4()
    names = {cat: "Groceries", other: "Household"}
    same = [row(GROCER, category_id=cat, apply_id=apply_id), row(GROCER, category_id=cat, apply_id=apply_id)]
    mixed = [row(GROCER, provider="Equity Bank", category_id=cat), row(GROCER, provider="Equity Bank", category_id=other)]

    g_same = groups_for(same, category_names=names)[0]
    assert (g_same["category_id"], g_same["category_name"], g_same["apply_id"]) == (cat, "Groceries", apply_id)
    g_mixed = groups_for(mixed, category_names=names)[0]
    assert g_mixed["category_id"] is None and g_mixed["apply_id"] is None


def test_suggestion_and_label_come_from_the_preview_rows():
    rows = [row(GROCER), row(GROCER)]
    groceries = {"category_id": uuid4(), "category_name": "Groceries", "is_new_category": False,
                 "source": "your_rule", "merchant_name": None}
    preview = {r["id"]: {"suggestion": groceries, "label": "Corner shop"} for r in rows}
    g = groups_for(rows, preview=preview)[0]
    assert g["suggestion"] == groceries and g["label"] == "Corner shop"


def test_group_suggestion_majority_wins_and_a_tie_gives_none():
    a = {"category_id": uuid4(), "category_name": "Groceries", "is_new_category": False, "source": "others"}
    b = {"category_id": uuid4(), "category_name": "Household", "is_new_category": False, "source": "others"}
    assert group_suggestion([a, a, b, None]) == a
    assert group_suggestion([a, b]) is None
    assert group_suggestion([None, None]) is None
    new = {"category_id": None, "category_name": "Pets", "is_new_category": True, "source": "directory"}
    assert group_suggestion([new, dict(new, category_name="pets")]) == new


def test_handled_groups_split_by_kind_and_carry_the_reason():
    rows = [row("Pay Bill Charge", handled_as="charge", handled_reason="M-Pesa charge", amount=10),
            row("Pay Bill Charge", handled_as="charge", handled_reason="M-Pesa charge", amount=10)]
    g = groups_for(rows)[0]
    assert g["handled_as"] == "charge" and g["handled_reason"] == "M-Pesa charge" and g["row_count"] == 2


# --- review tabs ----------------------------------------------------------


def test_review_tabs():
    assert review_tab(row(GROCER)) == "decide"
    assert review_tab(row(GROCER, handled_as="charge")) == "handled"
    assert review_tab(row(GROCER, allocated=100, applied_here=True)) == "decided"
    assert review_tab(row(GROCER, allocated=100, applied_here=False)) == "sorted"
    assert review_tab(row(GROCER, is_transfer=True)) == "sorted"
    assert _is_open(row(GROCER))


# --- filters, sort, pages -------------------------------------------------


def sample_groups():
    return [
        {"group_key": "a", "display_name": "Bravo", "direction": "out", "is_person": False, "row_count": 3, "total": 300},
        {"group_key": "b", "display_name": "alpha", "direction": "in", "is_person": True, "row_count": 1, "total": 900},
        {"group_key": "c", "display_name": "Charlie", "direction": "out", "is_person": True, "row_count": 3, "total": 50},
    ]


def test_default_sort_is_most_rows_then_amount():
    items, total = select_groups(sample_groups())
    assert [g["group_key"] for g in items] == ["a", "c", "b"] and total == 3


@pytest.mark.parametrize("sort,order,expected", [
    ("amount", "desc", ["b", "a", "c"]),
    ("amount", "asc", ["c", "a", "b"]),
    ("payee", "asc", ["b", "a", "c"]),
    ("rows", "asc", ["b", "c", "a"]),
])
def test_sorts(sort, order, expected):
    items, _ = select_groups(sample_groups(), sort=sort, order=order)
    assert [g["group_key"] for g in items] == expected


def test_filters():
    assert [g["group_key"] for g in select_groups(sample_groups(), direction="in")[0]] == ["b"]
    assert [g["group_key"] for g in select_groups(sample_groups(), kind="person")[0]] == ["c", "b"]
    assert [g["group_key"] for g in select_groups(sample_groups(), kind="business")[0]] == ["a"]


def search_groups():
    return [
        {"group_key": "a", "display_name": "ACME SUPERMARKET", "label": None, "number": "5555555",
         "direction": "out", "is_person": False, "row_count": 2, "total": 300,
         "rows": [{"description": "Merchant Payment to 5555555 - ACME SUPERMARKET"}]},
        {"group_key": "b", "display_name": "Pay Bill Online", "label": "Corner shop", "number": "888880",
         "direction": "out", "is_person": False, "row_count": 1, "total": 50,
         "rows": [{"description": "Pay Bill Online to 888880 - KPLC PREPAID"}]},
        {"group_key": "c", "display_name": "REVERSAL", "label": None, "number": None,
         "direction": "in", "is_person": False, "row_count": 1, "total": 10, "rows": [{"description": None}]},
    ]


@pytest.mark.parametrize("q,expected", [
    ("acme", ["a"]),            # payee name, any case
    ("corner", ["b"]),          # the user's label
    ("8888", ["b"]),            # the payee's number
    ("kplc", ["b"]),            # any statement line in the group
    ("  Acme  ", ["a"]),        # trimmed
    ("", ["a", "b", "c"]),      # blank = no search
    (None, ["a", "b", "c"]),
    ("nothing", []),
])
def test_search(q, expected):
    items, total = select_groups(search_groups(), q=q)
    assert [g["group_key"] for g in items] == expected and total == len(expected)


def test_search_combines_with_filters():
    assert select_groups(search_groups(), q="acme", direction="in")[0] == []


def test_pages():
    items, total = select_groups(sample_groups(), page=2, page_size=2)
    assert [g["group_key"] for g in items] == ["b"] and total == 3
    assert select_groups(sample_groups(), page=5, page_size=2) == ([], 3)


def test_status_never_reorders_the_list():
    # 2026-11-18: a payee the user files stays in place on All / Approved;
    # only the chosen sort decides the order.
    groups = [dict(g, status=s) for g, s in zip(sample_groups(), ["sorted", "decide", "handled"])]
    items, _ = select_groups(groups, sort="amount", order="desc")
    assert [g["group_key"] for g in items] == ["b", "a", "c"]


# --- ab-341: people by number ------------------------------------------------------

COOP = "Co-operative Bank"


def test_each_number_is_its_own_group():
    rows = [row("TRANSFER TO M-PESA 254700000001", provider=COOP),
            row("TRANSFER TO M-PESA 254700000001", provider=COOP),
            row("TRANSFER TO M-PESA 254700000002", provider=COOP)]
    groups = groups_for(rows)
    assert sorted(g["row_count"] for g in groups) == [1, 2]
    assert all(g["is_person"] for g in groups)


def test_a_group_takes_the_persons_name_from_another_row_with_the_same_number():
    rows = [row("TRANSFER TO M-PESA 254700000001", provider=COOP),
            row("MPS2ABCD123~0712345~254700000001~VERONICA~2026-01-31", provider=COOP, direction="in")]
    groups = groups_for(rows)
    assert {g["direction"]: g["display_name"] for g in groups} == {
        "out": "VERONICA · 254700000001", "in": "VERONICA · 254700000001"}


def test_without_a_name_anywhere_the_statement_text_shows():
    groups = groups_for([row("TRANSFER TO M-PESA 254700000001", provider=COOP)])
    assert groups[0]["display_name"] == "TRANSFER TO M-PESA 254700000001"


def test_mpesa_and_bank_rows_of_one_number_are_one_group():
    rows = [row("TRANSFER TO M-PESA 254700000001", provider=COOP),
            row("Customer Transfer to 254700000001 - JANE DOE")]
    groups = groups_for(rows)
    assert len(groups) == 1 and groups[0]["display_name"] == "JANE DOE · 254700000001"


# --- ab-343: one person, money in and out --------------------------------------

def test_both_directions_of_one_number_share_a_person_key():
    rows = [row("TRANSFER TO M-PESA 254700000001", provider=COOP),
            row("MPS2ABCD123~254700000001~0119283~MPESAC2B~JANE DOE", provider=COOP, direction="in")]
    groups = groups_for(rows)
    assert len(groups) == 2  # still allocated per direction
    keys = {g["person_key"] for g in groups}
    assert len(keys) == 1 and None not in keys
    assert "254700000001" not in keys.pop()


def test_same_name_different_numbers_are_different_people():
    rows = [row("MPS2ABCD123~7604987~254700000001~JANE~2026-01-07", provider=COOP, direction="in"),
            row("MPS2ABCD124~7604987~254700000002~JANE~2026-01-07", provider=COOP, direction="in"),
            row("TRANSFER TO M-PESA 254700000002", provider=COOP)]
    by_dir = {}
    for g in groups_for(rows):
        by_dir.setdefault(g["person_key"], set()).add(g["direction"])
    assert sorted(map(sorted, by_dir.values())) == [["in"], ["in", "out"]]


def test_businesses_name_only_people_and_handled_rows_have_no_person_key():
    rows = [row(GROCER),
            row("MPS 254700000001 UA7JH2ZRWZ JANE DOE 948FMNd", provider="Equity Bank", direction="in"),
            row("TRANSFER TO M-PESA 254700000003", provider=COOP, handled_as="transfer")]
    assert [g["person_key"] for g in groups_for(rows)] == [None, None, None]


def person_groups():
    # p1 out 3 rows/300, p1 in 2 rows/50, business 4 rows/100, p2 in 1 row/900
    return [
        {"group_key": "p1o", "person_key": "p1", "display_name": "Jane", "direction": "out", "is_person": True,
         "row_count": 3, "total": 300, "last_date": date(2026, 10, 1)},
        {"group_key": "biz", "person_key": None, "display_name": "Acme", "direction": "out", "is_person": False,
         "row_count": 4, "total": 100, "last_date": date(2026, 10, 2)},
        {"group_key": "p2i", "person_key": "p2", "display_name": "Bob", "direction": "in", "is_person": True,
         "row_count": 1, "total": 900, "last_date": date(2026, 10, 3)},
        {"group_key": "p1i", "person_key": "p1", "display_name": "Jane", "direction": "in", "is_person": True,
         "row_count": 2, "total": 50, "last_date": date(2026, 10, 4)},
    ]


def keys_of(items):
    return [g["group_key"] for g in items]


def test_a_persons_directions_sit_together_out_first_sorted_as_one():
    items, total = select_groups(person_groups())
    # Jane's 5 rows beat Acme's 4; out before in.
    assert keys_of(items) == ["p1o", "p1i", "biz", "p2i"] and total == 3


@pytest.mark.parametrize("sort,order,expected", [
    ("amount", "desc", ["p2i", "p1o", "p1i", "biz"]),   # Jane 350 > Acme 100
    ("payee", "asc", ["biz", "p2i", "p1o", "p1i"]),
    ("recent", "desc", ["p1o", "p1i", "p2i", "biz"]),   # Jane's latest row is 4 Oct
])
def test_person_bundles_sort_by_their_combined_values(sort, order, expected):
    assert keys_of(select_groups(person_groups(), sort=sort, order=order)[0]) == expected


def test_pages_count_people_and_never_split_one():
    first, total = select_groups(person_groups(), page=1, page_size=1)
    assert keys_of(first) == ["p1o", "p1i"] and total == 3
    assert keys_of(select_groups(person_groups(), page=2, page_size=1)[0]) == ["biz"]


def test_a_direction_filter_leaves_one_side():
    items, total = select_groups(person_groups(), direction="in")
    assert keys_of(items) == ["p1i", "p2i"] and total == 2


def test_the_coop_case_is_one_person_with_both_sides():
    out = [row("TRANSFER TO M-PESA 254700000001", provider=COOP) for _ in range(31)]
    inn = [row(f"SAO4ZIPB{i:02d}~254700000001~0119283~MPESAC2B_40 0200~JANE DOE", provider=COOP, direction="in")
           for i in range(24)]
    items, total = select_groups(groups_for(out + inn))
    assert total == 1
    assert [(g["direction"], g["row_count"]) for g in items] == [("out", 31), ("in", 24)]
    assert {g["display_name"] for g in items} == {"JANE DOE · 254700000001"}


def test_count_payees_counts_a_person_once():
    assert count_payees(person_groups()) == 3
