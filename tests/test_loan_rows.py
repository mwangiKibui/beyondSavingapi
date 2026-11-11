"""ab-260: loan repayment rows get a key the shared list maps to Loan
Repayment; payouts, loan requests, Fuliza draws/repayments and loan-ledger
rows get no key and a row_kind the review screen labels instead.

Formats from docs/research/loan-rows.md. Placeholder numbers and names only.
"""

from datetime import date
from uuid import uuid4

import pytest

from app.services.category_suggestions import Directory, build_preview_rows
from app.services.merchant_keys import extract_key
from tests.test_category_suggestions import merchant

SECRET = "test-secret"


def key(provider, description, direction="out"):
    return extract_key(provider=provider, description=description, direction=direction, secret=SECRET)


# --- NCBA ------------------------------------------------------------------


def test_ncba_loan_repayment_is_a_private_per_loan_key():
    car = key("NCBA Bank", "AA LOAN REPAYMENT 1000000001 AA26000AAAAA FT26000AAAAA")
    car_again = key("NCBA Bank", "AA LOAN REPAYMENT 1000000001 AA26000BBBBB FT26000BBBBB")
    personal = key("NCBA Bank", "AA LOAN REPAYMENT 1000000002 AA26000CCCCC FT26000CCCCC")
    assert car.kind == "reference" and car.provider == "NCBA Bank"
    assert car.value == car_again.value != personal.value
    assert "1000000001" not in car.value
    assert car.display == "Loan repayment · acc 1000000001"
    assert car.match_text == "AA LOAN REPAYMENT"
    assert not car.is_business and car.row_kind is None


def test_ncba_loan_key_never_collides_with_a_paybill_reference_of_the_same_digits():
    loan = key("NCBA Bank", "AA LOAN REPAYMENT 1234567 AA26000AAAAA FT26000AAAAA")
    ref = key("NCBA Bank", "IB OUTGOING MOBILE PAYMENTS PAYBILL FTX26000AAAAA 1234567")
    assert loan.value != ref.value


def test_ncba_loan_repayment_without_an_account_still_matches_the_list():
    k = key("NCBA Bank", "AA LOAN REPAYMENT")
    assert (k.kind, k.match_text) == ("name", "AA LOAN REPAYMENT")


# --- M-Pesa: M-Shwari and Fuliza ---------------------------------------------


def test_mshwari_repayment_keeps_its_name_key():
    k = key("M-Pesa", "M-Shwari Loan Repayment")
    assert (k.kind, k.value, k.match_text, k.row_kind) == (
        "name", "M-SHWARI LOAN REPAYMENT", "M-SHWARI LOAN REPAYMENT", None)


@pytest.mark.parametrize("description,direction,row_kind", [
    ("M-Shwari Loan Disburse", "in", "loan_payout"),
    ("M-Shwari Loan Request", "in", "loan_request"),
    ("OverDraft of Credit Party", "in", "overdraft_draw"),
    ("OD Loan Repayment to 232323 - M-PESA Overdraw", "out", "overdraft_repayment"),
])
def test_payouts_requests_and_fuliza_rows_get_no_key_and_a_row_kind(description, direction, row_kind):
    k = key("M-Pesa", description, direction)
    assert (k.kind, k.value, k.row_kind) == (None, None, row_kind)


@pytest.mark.parametrize("fuliza,plain", [
    ("Merchant Payment Fuliza M-Pesa to 5550001 - SAMPLE FUEL STATION LTD",
     "Merchant Payment to 5550001 - SAMPLE FUEL STATION LTD"),
    ("Pay Bill Online Fuliza M-Pesa to 555001 - SAMPLE POWER LTD Acc. 1234",
     "Pay Bill Online to 555001 - SAMPLE POWER LTD Acc. 1234"),
    ("Customer Transfer Fuliza MPesa to 254700000000 - JANE DOE",
     "Customer Transfer to 254700000000 - JANE DOE"),
    ("Airtime Purchase with Fuliza", "Airtime Purchase"),
])
def test_a_fuliza_paid_purchase_keys_like_the_same_purchase_paid_from_balance(fuliza, plain):
    f, p = key("M-Pesa", fuliza), key("M-Pesa", plain)
    assert (f.kind, f.value, f.name, f.account_hash) == (p.kind, p.value, p.name, p.account_hash)
    assert f.paid_with_overdraft and not p.paid_with_overdraft


# --- Mentor SACCO ---------------------------------------------------------------


def test_mentor_savings_ledger_loan_recovery_is_the_repayment():
    k = key("Mentor Sacco", "ATRC-00001 Loan Recovery:-Instant Loan")
    again = key("Mentor Sacco", "ATRC-00002 Loan Recovery:-Instant Loan")
    assert (k.kind, k.value, k.match_text) == ("name", "LOAN RECOVERY INSTANT LOAN", "SACCO LOAN RECOVERY INSTANT LOAN")
    assert k.value == again.value and k.row_kind is None


@pytest.mark.parametrize("description,direction,row_kind", [
    ("000000000001 Instant Loan...000000000001", "in", "loan_payout"),
    ("000000000001 Development Loan...000000000002", "in", "loan_payout"),
    ("ATRC-00001 Loan Recovery(Int. Paid -Instant Loan)", "out", "loan_ledger"),
    ("ATRC-00001 Loan Recovery(Prn. Paid -Instant Loan)", "out", "loan_ledger"),
    ("INT-19/08/25 Interest Due-82025", "in", "loan_ledger"),
    ("000000000001 Offseting Principal INSTANT LOAN-(00000000001)", "out", "loan_ledger"),
])
def test_mentor_payouts_and_loan_ledger_rows_get_no_key(description, direction, row_kind):
    k = key("Mentor Sacco", description, direction)
    assert (k.kind, k.row_kind) == (None, row_kind)


@pytest.mark.parametrize("description,value", [
    ("000000000001 Loan Apprisal Fee-Instant Loan-000000000001", "LOAN APPRISAL FEE"),
    ("000000000001 Loan Form-Instant Loan-000000000001", "LOAN FORM"),
    ("000000000001 Sms charges-Instant Loan-000000000001", "SMS CHARGES"),
    ("000000000001 Insurance Fee-Instant Loan", "INSURANCE FEE"),
    ("000000000001 Guarantor Notification-Instant Loan-000000000001", "GUARANTOR NOTIFICATION"),
    ("000000000001 Excise Duty Payable-Loan No.000000000001", "EXCISE DUTY PAYABLE"),
])
def test_mentor_loan_fees_are_name_keys_under_one_shared_pattern(description, value):
    k = key("Mentor Sacco", description)
    assert (k.kind, k.value, k.row_kind) == ("name", value, None)
    assert k.match_text == f"SACCO LOAN FEE {value}"


def test_other_mentor_rows_are_unchanged():
    k = key("Mentor Sacco", "SSPSP00AAAA000001 Balance Enquiry Charges-SSPSP00AAAA000001")
    assert (k.kind, k.value) == ("name", "BALANCE ENQUIRY CHARGES")
    assert key("Mentor Sacco", "SJN-00001 RISK FUND AUGUST 2025").value == "RISK FUND"


# --- preview ------------------------------------------------------------------------


def txn(description, direction="out", provider_rows=None):
    return {"id": uuid4(), "description": description, "direction": direction, "amount": 1000.0,
            "txn_date": date(2026, 10, 1), "currency": "KES", "allocated": 0, "category_id": None,
            "is_transfer": False, "balance_after": None}


def preview(provider, rows, directory):
    keys = {r["id"]: key(provider, r["description"], r["direction"]) for r in rows}
    return build_preview_rows(rows, keys=keys, rules=[], directory=directory, categories=[])


LOANS = Directory.from_rows(
    merchant("Loan repayments", "Loan Repayment",
             patterns=["AA LOAN REPAYMENT", "M-SHWARI LOAN REPAYMENT", "SACCO LOAN RECOVERY"])
    + merchant("SACCO loan fees", "Bank & mobile charges", patterns=["SACCO LOAN FEE"]),
    blocked=[],
)


def test_first_time_user_sees_loan_repayment_suggested_from_the_list():
    ncba = preview("NCBA Bank", [txn("AA LOAN REPAYMENT 1000000001 AA26000AAAAA FT26000AAAAA")], LOANS)[0]
    mshwari = preview("M-Pesa", [txn("M-Shwari Loan Repayment")], LOANS)[0]
    mentor = preview("Mentor Sacco", [txn("ATRC-00001 Loan Recovery:-Instant Loan")], LOANS)[0]
    for row in (ncba, mshwari, mentor):
        assert row["suggestion"]["category_name"] == "Loan Repayment"
        assert row["suggestion"]["source"] == "directory"
        assert row["suggestion"]["is_new_category"] is True
    assert ncba["display_name"] == "Loan repayment · acc 1000000001"
    assert ncba["key_kind"] == "reference"


def test_two_ncba_loans_are_two_groups():
    rows = [txn("AA LOAN REPAYMENT 1000000001 AA26000AAAAA FT26000AAAAA"),
            txn("AA LOAN REPAYMENT 1000000001 AA26000BBBBB FT26000BBBBB"),
            txn("AA LOAN REPAYMENT 1000000002 AA26000CCCCC FT26000CCCCC")]
    out = preview("NCBA Bank", rows, LOANS)
    assert out[0]["group_key"] == out[1]["group_key"] != out[2]["group_key"]


def test_mentor_loan_fees_suggest_charges():
    out = preview("Mentor Sacco", [txn("000000000001 Loan Apprisal Fee-Instant Loan-000000000001")], LOANS)[0]
    assert out["suggestion"]["category_name"] == "Bank & mobile charges"


def test_payouts_and_fuliza_rows_preview_with_a_row_kind_and_no_suggestion():
    rows = [txn("M-Shwari Loan Disburse", "in"), txn("OverDraft of Credit Party", "in"),
            txn("OD Loan Repayment to 232323 - M-PESA Overdraw")]
    out = preview("M-Pesa", rows, LOANS)
    assert [r["row_kind"] for r in out] == ["loan_payout", "overdraft_draw", "overdraft_repayment"]
    assert all(r["suggestion"] is None for r in out)


def test_paid_with_overdraft_flag_reaches_the_preview():
    out = preview("M-Pesa", [txn("Merchant Payment Fuliza M-Pesa to 5550001 - SAMPLE FUEL STATION LTD"),
                             txn("Merchant Payment to 5550001 - SAMPLE FUEL STATION LTD")], LOANS)
    assert [r["paid_with_overdraft"] for r in out] == [True, False]
    assert [r["row_kind"] for r in out] == [None, None]
