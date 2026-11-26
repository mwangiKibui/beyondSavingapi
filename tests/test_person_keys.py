"""ab-341: people are keyed by who they are, on every statement.

Identity, best first: a full phone number, else the visible digits of a
masked number plus the name, else the name. The key is a keyed hash in a
provider-independent namespace, so the same number on M-Pesa and on a
bank statement is the same person. Placeholder numbers and names only."""

import pytest

from app.services.merchant_keys import MPESA, NCBA, PERSON, extract_key, hash_value, normalize_text

SECRET = "test-secret"
COOP = "Co-operative Bank"


def key(provider: str, description: str, direction: str = "out"):
    return extract_key(provider=provider, description=description, direction=direction, secret=SECRET)


# --- full numbers -------------------------------------------------------------


@pytest.mark.parametrize("written", ["254700000001", "0700000001", "+254700000001", "+254 700000001"])
def test_a_full_number_is_the_same_person_however_it_is_written(written):
    base = key(COOP, "TRANSFER TO M-PESA 254700000001")
    assert key(COOP, f"TRANSFER TO M-PESA {written}").value == base.value


def test_different_numbers_are_different_people():
    a = key(COOP, "TRANSFER TO M-PESA 254700000001")
    b = key(COOP, "TRANSFER TO M-PESA 254700000002")
    assert a.is_person and b.is_person
    assert a.kind == "reference" and a.provider == PERSON
    assert a.value != b.value


def test_the_same_number_on_mpesa_and_a_bank_is_one_person():
    mpesa = key(MPESA, "Customer Transfer to 254700000001 - JANE DOE")
    coop = key(COOP, "TRANSFER TO M-PESA 254700000001")
    assert mpesa.value == coop.value and mpesa.provider == coop.provider == PERSON


def test_a_full_number_wins_over_the_name():
    a = key(MPESA, "Customer Transfer to 254700000001 - JANE DOE")
    b = key(MPESA, "Customer Transfer to 254700000001 - J DOE")
    assert a.value == b.value


def test_a_packed_incoming_row_is_keyed_by_its_number():
    a = key(COOP, "MPS2ABCD123~0712345~254700000001~VERONICA~2026-01-31", "in")
    b = key(COOP, "MPS9ZZZZ999~0799999~254700000001~VERONICA~2026-02-14", "in")
    assert a.is_person and a.value == b.value
    assert a.value == key(COOP, "TRANSFER TO M-PESA 254700000001").value
    assert a.person_name == "VERONICA"


def test_ncba_incoming_person_uses_the_number():
    k = key(NCBA, "MPESA PAYBILL TRANSFER MPESA REF: UHC0000000 254700000001 JANE DOE ABCDEF12 FTC2608ABCD", "in")
    assert k.provider == PERSON
    assert k.value == key(COOP, "TRANSFER TO M-PESA 254700000001").value


# --- masked numbers -------------------------------------------------------------


def test_a_masked_number_with_a_name_is_keyed_by_both():
    a = key(MPESA, "Customer Transfer to 0712***678 - JANE DOE")
    same = key(MPESA, "Customer Transfer to 254712***678 - JANE DOE")
    other_digits = key(MPESA, "Customer Transfer to 0712***679 - JANE DOE")
    other_name = key(MPESA, "Customer Transfer to 0712***678 - JOHN DOE")
    assert a.is_person and a.provider == PERSON
    assert a.value == same.value
    assert a.value != other_digits.value
    assert a.value != other_name.value


def test_name_only_when_no_digits_are_shown():
    a = key(MPESA, "Customer Transfer to - JANE DOE")
    b = key(MPESA, "Customer Transfer to - JANE DOE")
    assert a.is_person and a.value == b.value
    assert a.value != key(MPESA, "Customer Transfer to - JOHN DOE").value


# --- what the uploader sees ---------------------------------------------------------


def test_the_display_is_the_name_and_the_number_as_printed():
    k = key(MPESA, "Customer Transfer to 254700000001 - JANE DOE")
    assert k.person_name == "JANE DOE"
    assert k.person_number == "254700000001"


def test_a_masked_statement_number_shows_as_printed():
    k = key(MPESA, "Customer Transfer to 0712***678 - JANE DOE")
    assert k.person_number == "0712***678"


def test_nothing_raw_is_in_the_key():
    k = key(COOP, "MPS2ABCD123~0712345~254700000001~VERONICA~2026-01-31", "in")
    assert "254700000001" not in k.value and "VERONICA" not in k.value
    assert k.match_text == "" and not k.is_business


# --- the generic reader -------------------------------------------------------------


def test_a_business_row_with_a_phone_stays_a_business():
    k = key(COOP, "PAYMENT TO ACME HARDWARE LTD 0700000001")
    assert not k.is_person and k.kind == "name"


def test_rows_without_a_number_are_unchanged():
    k = key(COOP, "TRANSFER TO M-PESA")
    assert not k.is_person and k.kind == "name"


def test_short_numbers_are_not_phones():
    k = key(COOP, "CHEQUE DEPOSIT 0712345 BRANCH")
    assert not k.is_person


# --- remembered choices made before ab-341 -------------------------------------------


def test_mpesa_person_keys_carry_their_old_key():
    text = "Customer Transfer to 254700000001 - JANE DOE"
    k = key(MPESA, text)
    cleaned = normalize_text(text)
    old_identity = cleaned[len("CUSTOMER TRANSFER "):]
    assert k.legacy_provider == MPESA
    assert k.legacy_value == hash_value(MPESA, old_identity, secret=SECRET)


def test_bank_person_keys_have_no_old_key():
    assert key(COOP, "TRANSFER TO M-PESA 254700000001").legacy_value is None
