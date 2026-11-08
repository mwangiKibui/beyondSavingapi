"""ab-253: turning one statement row into a suggestion key.

Fixtures follow the formats documented in docs/research/kenya-merchants/
parts/descriptor-formats.md, with placeholder names, phones and
references only - never real statement values.
"""

import pytest

from app.services.merchant_keys import (
    extract_key,
    hash_value,
    looks_like_business,
    normalize_text,
    strip_reference_codes,
)

SECRET = "test-secret"


def key(provider: str, description: str, direction: str = "out"):
    return extract_key(provider=provider, description=description, direction=direction, secret=SECRET)


# --- helpers ------------------------------------------------------------


def test_normalize_text_uppercases_and_collapses_spaces():
    assert normalize_text("  Pay  Bill\nto  888880 ") == "PAY BILL TO 888880"


def test_normalize_text_handles_none():
    assert normalize_text(None) == ""


def test_strip_reference_codes_removes_mixed_letter_digit_tokens():
    assert strip_reference_codes("MERCHANT ATM TRANSACTION FTC2608PBDMPG SHELL KINGS") == "MERCHANT ATM TRANSACTION SHELL KINGS"


def test_strip_reference_codes_keeps_plain_words():
    assert strip_reference_codes("KPLC PREPAID") == "KPLC PREPAID"


def test_hash_value_is_keyed_and_stable():
    a = hash_value("ncba", "123456", secret=SECRET)
    assert a == hash_value("ncba", "123456", secret=SECRET)
    assert a != hash_value("ncba", "123456", secret="other-secret")
    assert a != hash_value("equity", "123456", secret=SECRET)
    assert "123456" not in a
    assert a.startswith("h:")


@pytest.mark.parametrize(
    "name",
    ["NAIVAS LTD", "KPLC PREPAID LIMITED", "PAZA BARBER SPA AND BEAUTY PARLOR", "ST MARKS PARISH", "ACME SUPERMARKET"],
)
def test_looks_like_business_true(name):
    assert looks_like_business(name)


@pytest.mark.parametrize("name", ["JOHN KAMAU", "MARY WANJIKU NJERI", "PETER OMONDI", ""])
def test_looks_like_business_false_for_person_names(name):
    assert not looks_like_business(name)


# --- M-Pesa -------------------------------------------------------------


def test_mpesa_paybill_with_account_reference():
    k = key("M-Pesa", "Pay Bill Online to 888880 - KPLC PREPAID Acc. 01234567890")
    assert (k.kind, k.value) == ("paybill", "888880")
    assert k.number_kind == "paybill" and k.number == "888880"
    assert k.name == "KPLC PREPAID"
    assert k.is_business
    assert k.account_hash is not None and "01234567890" not in k.account_hash


def test_mpesa_paybill_fuliza_wording_is_ignored():
    plain = key("M-Pesa", "Pay Bill to 880100 - ACME BANK KENYA PLC. Acc 555")
    fuliza = key("M-Pesa", "Pay Bill Online Fuliza M-Pesa to 880100 - ACME BANK KENYA PLC. Acc. 555")
    assert (plain.kind, plain.value, plain.name) == (fuliza.kind, fuliza.value, fuliza.name)
    assert plain.name == "ACME BANK KENYA PLC"
    assert plain.account_hash == fuliza.account_hash


def test_mpesa_merchant_payment_business_till():
    k = key("M-Pesa", "Merchant Payment Fuliza M-Pesa to 7700590 - PAZA BARBER SPA AND BEAUTY PARLOR")
    assert (k.kind, k.value, k.number_kind) == ("till", "7700590", "till")
    assert k.name == "PAZA BARBER SPA AND BEAUTY PARLOR"
    assert k.is_business


def test_mpesa_merchant_payment_till_registered_to_a_person_is_private():
    k = key("M-Pesa", "Merchant Payment Fuliza M-Pesa to 7286859 - PETER OMONDI")
    assert (k.kind, k.value) == ("till", "7286859")
    assert not k.is_business  # never votes into the shared list


def test_mpesa_business_payment_in():
    k = key("M-Pesa", "Business Payment from 329299 - ACME BANK via API. Original conversation ID is 9F8E7D-KE-IBNK", "in")
    assert (k.kind, k.value, k.name) == ("paybill", "329299", "ACME BANK")


def test_mpesa_data_bundles_number_and_name_run_together():
    k = key("M-Pesa", "Recharge for Customer With Fuliza to 4093441SAFARICOM DATA BUNDLES by - 254700***000 JANE DOE")
    assert (k.kind, k.value) == ("paybill", "4093441")
    assert k.name == "SAFARICOM DATA BUNDLES"
    assert "JANE" not in (k.name or "") and "JANE" not in k.match_text


def test_mpesa_postpaid_bundles():
    k = key("M-Pesa", "Customer Bundle Purchase with Fuliza to 244441SAFARICOM POSTPAID BUNDLES by - 254700***000 JANE DOE")
    assert (k.kind, k.value, k.name) == ("paybill", "244441", "SAFARICOM POSTPAID BUNDLES")


def test_mpesa_airtime_purchase_is_a_name_key():
    k = key("M-Pesa", "Airtime Purchase with Fuliza")
    assert (k.kind, k.value) == ("name", "AIRTIME PURCHASE")
    assert k.is_business


def test_mpesa_charge_rows_are_name_keys():
    assert key("M-Pesa", "Pay Bill Charge").value == "PAY BILL CHARGE"
    assert key("M-Pesa", "Customer Transfer of Funds Charge").value == "CUSTOMER TRANSFER OF FUNDS CHARGE"


def test_mpesa_person_transfer_is_a_private_hashed_reference():
    k = key("M-Pesa", "Customer Transfer Fuliza MPesa to 254700000000 - JOHN KAMAU")
    assert k.kind == "reference"
    assert k.provider == "M-Pesa"
    assert "JOHN" not in k.value and "254700000000" not in k.value
    assert not k.is_business


def test_mpesa_fuliza_overdraft_rows_get_no_key():
    assert key("M-Pesa", "OverDraft of Credit Party", "in").kind is None
    assert key("M-Pesa", "OD Loan Repayment to 232323 - M-PESA Overdraw").kind is None


# --- NCBA ---------------------------------------------------------------


def test_ncba_paybill_row_is_account_reference_not_paybill():
    k = key("NCBA Bank", "IB Outgoing Mobile Payments PayBill FTX26232ABCDE 123456")
    assert k.kind == "reference"
    assert k.provider == "NCBA Bank"
    assert "123456" not in k.value
    assert k.number is None  # the number is the payer's account reference
    assert not k.is_business
    assert k.display == "Paybill payment · ref 123456"


def test_ncba_same_reference_gives_same_key_across_rows():
    a = key("NCBA Bank", "IB Outgoing Mobile Payments PayBill FTX26232ABCDE 123456")
    b = key("NCBA Bank", "IB Outgoing Mobile Payments PayBill FTX26241VWXYZ 123456")
    assert a.value == b.value


def test_ncba_buygoods_row_is_a_till():
    k = key("NCBA Bank", "IB Outgoing Mobile Payments BuyGoods FTX26216ABCDE 9120385")
    assert (k.kind, k.value, k.number_kind, k.number) == ("till", "9120385", "till", "9120385")
    assert k.name is None
    assert k.display == "Till 9120385"


def test_ncba_utility_payment_uses_the_biller_name():
    k = key("NCBA Bank", "UTILITY PAYMENT KPLC PREPAID 37123456789 AA26232ABCD FTX26232ABCDE")
    assert (k.kind, k.value) == ("name", "KPLC PREPAID")
    assert k.is_business


def test_ncba_card_payment_uses_the_merchant_name():
    k = key("NCBA Bank", "MERCHANT ATM TRANSACTION FTC2608PBDMPG SHELL KINGONGO SERVICNYERI 400000......1234")
    assert k.kind == "name"
    assert k.value.startswith("SHELL")
    assert "1234" not in k.value
    assert "SHELL" in k.match_text


def test_ncba_transfer_to_own_mpesa_gets_no_key():
    k = key("NCBA Bank", "TRF TO M-PESA 254700000000 FTC260813ABCD")
    assert k.kind is None
    assert k.is_transfer_hint


def test_ncba_charges_are_name_keys():
    assert key("NCBA Bank", "MOBILE PAYMENTS CHARGES 254700000000 FTC260813ABCD").value == "MOBILE PAYMENTS CHARGES"


def test_ncba_loan_rows_get_no_key():
    assert key("NCBA Bank", "AA LOAN REPAYMENT 1234567 AA26232ABCD FT26232ABCDE").kind is None


def test_ncba_incoming_person_payment_is_private():
    k = key("NCBA Bank", "MPESA PAYBILL TRANSFER MPESA REF: UHC0000000 254700000000 JANE DOE ABCDEF12 FTC2608ABCD", "in")
    assert k.kind == "reference"
    assert not k.is_business
    assert "JANE" not in k.value


# --- fallback (Equity, Mentor Sacco, unknown layouts) ---------------------


def test_fallback_strips_refs_and_digits_into_a_private_name_key():
    k = key("Equity Bank", "U5C3A6F8A0011 SMS CHARGE")
    assert (k.kind, k.value) == ("name", "SMS CHARGE")


def test_fallback_mentor_sacco_fee():
    k = key("Mentor Sacco", "SJN-1234 RISK FUND MARCH 2026")
    assert k.kind == "name"
    assert k.value.startswith("RISK FUND")


def test_empty_description_gets_no_key():
    assert key("Equity Bank", "").kind is None


def test_ncba_card_match_text_keeps_the_prefix_for_shared_patterns():
    k = key("NCBA Bank", "MERCHANT ATM TRANSACTION FTC2608PBDMPG GOOGLE ONE 400000......1234")
    assert k.value == "GOOGLE ONE"
    assert k.match_text == "MERCHANT ATM TRANSACTION GOOGLE ONE"


def test_mpesa_person_transfer_has_no_match_text():
    assert key("M-Pesa", "Customer Transfer to 254700000000 - KFC MWANGI").match_text == ""
