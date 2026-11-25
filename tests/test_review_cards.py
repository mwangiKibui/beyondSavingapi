"""ab-335: which Allocation card a review row counts under."""

from app.services.payee_groups import card_counts, group_cards, review_tab, row_card


def test_a_row_handled_at_import_waits_to_be_accepted():
    assert review_tab({"handled_as": "charge", "apply_id": None}) == "handled"
    assert review_tab({"handled_as": "charge", "apply_id": None, "handling_accepted_at": "x"}) == "accepted"


def test_cards():
    g = {"suggestion": None}
    assert row_card(g, {"status": "decide", "suggestion": None}) == "pending"
    assert row_card(g, {"status": "decide", "suggestion": {"category_id": "c"}}) == "suggested"
    # A line without its own suggestion takes the payee group's, as Approve does.
    assert row_card({"suggestion": {"category_id": "c"}}, {"status": "decide", "suggestion": None}) == "suggested"
    assert row_card(g, {"status": "handled"}) == "suggested"
    for status in ("decided", "sorted", "accepted"):
        assert row_card(g, {"status": status}) == "approved"


def test_counts_and_a_group_spanning_two_cards():
    mixed = {"suggestion": None, "rows": [{"status": "decide", "suggestion": None}, {"status": "decided"}]}
    handled = {"suggestion": None, "rows": [{"status": "handled"}, {"status": "accepted"}]}
    assert group_cards(mixed) == {"pending", "approved"}
    assert card_counts([mixed, handled]) == {"pending": 1, "suggested": 1, "approved": 2}
