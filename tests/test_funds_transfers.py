"""ab-291: the Funds transfers report - one row per transfer, From -> To,
totals per account. Pure shaping over get_transfers-shaped rows; the SQL
is checked in the live run."""

from datetime import date
from decimal import Decimal
from uuid import uuid4

from app.repositories.reports import build_funds_transfers, funds_transfer_totals

MPESA, BANK, SACCO = uuid4(), uuid4(), uuid4()


def row(*, account_id=MPESA, account_name="M-Pesa", direction="out", amount="5000.00", other=None,
        other_is_transfer=False, sub_ledger_name=None, handled_reason=None, reason="Between my accounts",
        source_account_id=None, source_description=None, source_sub_ledger=None, txn_date=date(2026, 11, 3)):
    tid = uuid4()
    r = {
        "id": uuid4(), "transaction_id": tid, "amount": Decimal(amount), "currency": "KES",
        "source_account_id": source_account_id, "source_account_nickname": "Mentor Sacco" if source_account_id else None,
        "source_sub_ledger_id": None, "source_sub_ledger_name": None, "source_sub_ledger_account_id": None,
        "source_sub_ledger_account_nickname": None, "source_description": source_description,
        "transfer_reason_name": reason, "direction": direction, "date": txn_date, "handled_reason": handled_reason,
        "account_id": account_id, "account_name": account_name, "sub_ledger_name": sub_ledger_name,
        "other_id": None, "other_direction": None, "other_account_id": None, "other_account_name": None,
        "other_sub_ledger_name": None, "other_is_transfer": other_is_transfer,
    }
    if source_sub_ledger:
        r.update(source_sub_ledger_id=uuid4(), source_sub_ledger_name=source_sub_ledger[1],
                 source_sub_ledger_account_id=SACCO, source_sub_ledger_account_nickname=source_sub_ledger[0])
    if other:
        r.update(other_id=other["transaction_id"], other_direction=other["direction"],
                 other_account_id=other["account_id"], other_account_name=other["account_name"],
                 other_sub_ledger_name=other.get("sub_ledger_name"))
    return r


def pair(amount="5000.00"):
    """An M-Pesa -> bank transfer with both statements uploaded."""
    out = row(amount=amount, handled_reason="Sent to your NCBA account")
    inn = row(account_id=BANK, account_name="NCBA", direction="in", amount=amount,
              handled_reason="Received from your M-Pesa account")
    out.update(other_id=inn["transaction_id"], other_direction="in", other_account_id=BANK,
               other_account_name="NCBA", other_sub_ledger_name=None, other_is_transfer=True)
    inn.update(other_id=out["transaction_id"], other_direction="out", other_account_id=MPESA,
               other_account_name="M-Pesa", other_sub_ledger_name=None, other_is_transfer=True)
    return out, inn


def test_a_pair_is_listed_once_from_the_money_out_side():
    out, inn = pair()
    for rows in ([out, inn], [inn, out]):
        items = build_funds_transfers(rows)
        assert [(i["from_label"], i["to_label"], i["reason"]) for i in items] == [
            ("M-Pesa", "NCBA", "Sent to your NCBA account")
        ]
        assert (items[0]["from_account_id"], items[0]["to_account_id"]) == (MPESA, BANK)


def test_a_pair_whose_other_side_is_outside_the_dates_still_shows():
    out, inn = pair()
    items = build_funds_transfers([inn])
    assert [(i["from_label"], i["to_label"]) for i in items] == [("M-Pesa", "NCBA")]


def test_money_out_with_no_pair_goes_from_the_account_to_what_it_names():
    items = build_funds_transfers([row(source_description="NCBA")])
    assert [(i["from_label"], i["to_label"], i["to_account_id"]) for i in items] == [("M-Pesa", "NCBA", None)]


def test_money_in_with_no_pair_comes_from_what_it_names_to_the_account():
    items = build_funds_transfers([row(account_id=BANK, account_name="NCBA", direction="in",
                                       source_description="M-Pesa")])
    assert [(i["from_label"], i["from_account_id"], i["to_label"]) for i in items] == [("M-Pesa", None, "NCBA")]


def test_a_sacco_move_is_one_row_between_sub_ledgers():
    savings = {"transaction_id": uuid4(), "direction": "in", "account_id": SACCO, "account_name": "Mentor Sacco",
               "sub_ledger_name": "Ordinary Deposit"}
    spending = row(account_id=SACCO, account_name="Mentor Sacco", sub_ledger_name="FOSA", source_account_id=SACCO,
                   other=savings, handled_reason="Moved to your Ordinary Deposit")
    items = build_funds_transfers([spending])
    assert [(i["from_label"], i["to_label"], i["reason"]) for i in items] == [
        ("Mentor Sacco / FOSA", "Mentor Sacco / Ordinary Deposit", "Moved to your Ordinary Deposit")
    ]


def test_a_tracked_source_sub_ledger_names_its_account():
    items = build_funds_transfers([row(direction="in", source_sub_ledger=("Mentor Sacco", "FOSA"))])
    assert (items[0]["from_label"], items[0]["from_account_id"]) == ("Mentor Sacco / FOSA", SACCO)


def test_the_reason_falls_back_to_the_transfer_reason():
    assert build_funds_transfers([row(source_description="NCBA")])[0]["reason"] == "Between my accounts"


def test_account_filter_keeps_either_side():
    out, inn = pair()
    other = row(source_description="Cash")
    assert len(build_funds_transfers([out, inn, other], account_id=BANK)) == 1
    assert len(build_funds_transfers([out, inn, other], account_id=MPESA)) == 2
    assert build_funds_transfers([out, inn, other], account_id=uuid4()) == []


def test_totals_add_up_sent_and_received_per_account():
    out, inn = pair("5000.00")
    items = build_funds_transfers([out, inn, row(amount="200.00", source_description="Cash")])
    totals = funds_transfer_totals(items)
    assert [(t["account_name"], t["sent"], t["received"]) for t in totals] == [
        ("M-Pesa", Decimal("5200.00"), Decimal("0")),
        ("NCBA", Decimal("0"), Decimal("5000.00")),
    ]
