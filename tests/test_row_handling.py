"""ab-281: which statement rows are handled at import without asking."""

import pytest

from app.services.merchant_keys import EQUITY, MENTOR, MPESA, NCBA, extract_key
from app.services.row_handling import (
    BETWEEN_MY_ACCOUNTS,
    CHARGES_CATEGORY,
    LOAN_REPAYMENT_CATEGORY,
    classify_row,
)

SECRET = "test-secret"


def classify(provider, description, direction="out", owned=frozenset({MPESA, NCBA})):
    key = extract_key(provider=provider, description=description, direction=direction, secret=SECRET)
    return classify_row(provider=provider, description=description, direction=direction, key=key, owned_providers=owned)


@pytest.mark.parametrize(
    ("provider", "description"),
    [
        (MPESA, "Pay Bill Charge"),
        (MPESA, "Customer Transfer of Funds Charge"),
        (NCBA, "MOBILE PAYMENTS CHARGES 000000000000 FTC000000AAAA"),
        (NCBA, "KE EXCISE DUTY CHG00000A00A0 DR CARD CHARGE 00000000"),
        (NCBA, "INWARD DIRECT DEBIT COMMISSION AC-KES0000000000000"),
        (EQUITY, "SMS CHARGE U0A0A0A0A0000"),
        (EQUITY, "INTERIM STATEMENT PRINTING CHARGES INTERIM STATMNT PRINT CHRGS"),
        (MENTOR, "SJN-0001 SMS Charges"),
    ],
)
def test_charges_are_filed_under_bank_and_mobile_charges(provider, description):
    handling = classify(provider, description)
    assert handling.kind == "charge"
    assert handling.category == CHARGES_CATEGORY
    assert handling.reason


def test_excise_duty_says_so():
    assert classify(NCBA, "EXCISE DUTY CHG00000AA0AA FT00000AAA0A").reason == "Excise duty on a charge"


def test_a_charge_coming_back_in_is_left_for_the_user():
    assert classify(MPESA, "Pay Bill Charge", direction="in") is None


@pytest.mark.parametrize(
    ("description", "direction", "reason_word"),
    [
        ("M-Shwari Loan Disburse", "in", "Borrowed"),
        ("M-Shwari Loan Request", "in", "request"),
        ("OverDraft of Credit Party", "in", "Fuliza"),
        ("OD Loan Repayment to 000000 - M-PESA OVERDRAW", "out", "Fuliza"),
    ],
)
def test_loan_and_fuliza_rows_get_no_category(description, direction, reason_word):
    handling = classify(MPESA, description, direction)
    assert handling.kind == "loan"
    assert handling.category is None
    assert reason_word in handling.reason


@pytest.mark.parametrize(
    ("provider", "description"),
    [
        (MPESA, "M-Shwari Loan Repayment"),
        (NCBA, "AA LOAN REPAYMENT 0000000000 AA00000AAAA0 FT00000A0A0A"),
        (MENTOR, "ATRC-0001 Loan Recovery:- Development Loan"),
    ],
)
def test_loan_repayments_get_the_loan_repayment_category(provider, description):
    handling = classify(provider, description)
    assert handling.kind == "loan"
    assert handling.category == LOAN_REPAYMENT_CATEGORY


def test_sacco_loan_ledger_rows_are_loans():
    assert classify(MENTOR, "0001 Interest Due 01/01/2026", owned=frozenset({MENTOR})).kind == "loan"


def test_bank_to_mpesa_is_a_transfer_waiting_for_mpesa():
    handling = classify(NCBA, "TRF TO M-PESA 000000000000 FTC000000AAAA")
    assert handling.kind == "transfer"
    assert handling.target_provider == MPESA


def test_mpesa_paybill_to_a_bank_the_user_has_is_a_transfer():
    for text in (
        "Pay Bill Online to 000000 - NCBA BANK KENYA PLC. Acc. 0000000000",
        "Pay Bill Online Fuliza M-Pesa to 000000 - NCBA BANK KENYA PLC. Acc. 0000000000",
    ):
        handling = classify(MPESA, text)
        assert handling.kind == "transfer"
        assert handling.target_provider == NCBA
        assert "NCBA Bank" in handling.reason


def test_mpesa_money_in_from_the_users_bank_is_a_transfer():
    handling = classify(MPESA, "Business Payment from 000000 - NCBA BANK KENYA PLC via API", direction="in")
    assert handling.kind == "transfer"
    assert handling.target_provider == NCBA


def test_mpesa_paybill_to_a_bank_the_user_does_not_have_is_left_alone():
    assert classify(MPESA, "Pay Bill Online to 000000 - NCBA BANK KENYA PLC. Acc. 0000000000", owned=frozenset({MPESA})) is None


@pytest.mark.parametrize(
    "description",
    [
        "Customer Transfer to - 2547******00 JOHN DOE",
        "Customer Transfer Fuliza MPesa to - 2547******00 JOHN DOE",
        "Merchant Payment to 0000000 - SOME SHOP LTD",
        "Pay Bill Online to 000000 - KPLC PREPAID. Acc. 00000000000",
    ],
)
def test_people_and_ordinary_purchases_are_never_handled(description):
    assert classify(MPESA, description) is None


def test_constants():
    assert CHARGES_CATEGORY == "Bank & mobile charges"
    assert LOAN_REPAYMENT_CATEGORY == "Loan Repayment"
    assert BETWEEN_MY_ACCOUNTS == "Between my accounts"


# --- golden counts on the sample statements in tests/fixtures ---------------

from collections import Counter  # noqa: E402

from app.parsers.equity_bank import parse_equity_bank_statement  # noqa: E402
from app.parsers.mpesa import parse_mpesa_statement  # noqa: E402
from app.parsers.ncba_bank import parse_ncba_bank_statement  # noqa: E402
from app.services.statement_files import decrypt_if_needed  # noqa: E402
from tests import test_parsers_equity_bank, test_parsers_mpesa, test_parsers_ncba_bank  # noqa: E402


def _golden(provider, module, parse):
    content = decrypt_if_needed(
        content=module.FIXTURE.read_bytes(), filename=module.FIXTURE.name, password=module.FIXTURE_PASSWORD
    )
    counts = Counter()
    for row in parse(content):
        handling = classify(provider, row["description"], row["direction"])
        counts[handling.kind if handling else "review"] += 1
    return counts


def test_golden_mpesa_sample():
    # 21 loan/Fuliza rows + 1 M-Shwari repayment, 6 charges, 4 Paybill
    # payments to the user's own NCBA account; the rest are real choices.
    assert _golden(MPESA, test_parsers_mpesa, parse_mpesa_statement) == Counter(
        loan=22, charge=6, transfer=4, review=12
    )


def test_golden_ncba_sample():
    assert _golden(NCBA, test_parsers_ncba_bank, parse_ncba_bank_statement) == Counter(
        charge=27, transfer=19, loan=2, review=31
    )


def test_golden_equity_sample():
    assert _golden(EQUITY, test_parsers_equity_bank, parse_equity_bank_statement) == Counter(charge=2, review=4)


# --- plan_pairs --------------------------------------------------------------

from datetime import date  # noqa: E402
from uuid import uuid4  # noqa: E402

from app.services.row_handling import plan_pairs  # noqa: E402

NCBA_ACC, MPESA_ACC = uuid4(), uuid4()


def row(provider, direction, amount, day, *, handled_as=None, target=None):
    return {
        "id": uuid4(),
        "account_id": NCBA_ACC if provider == NCBA else MPESA_ACC,
        "provider": provider,
        "direction": direction,
        "amount": amount,
        "txn_date": date(2026, 10, day),
        "handled_as": handled_as,
        "target_provider": target,
    }


def test_bank_to_mpesa_pairs_with_the_mpesa_receipt():
    leg = row(NCBA, "out", 500.0, 5, handled_as="transfer", target=MPESA)
    receipt = row(MPESA, "in", 500.0, 5)
    assert plan_pairs([leg], [leg, receipt]) == [(leg, receipt)]


def test_pairs_in_either_upload_order_when_both_legs_are_waiting():
    mpesa_leg = row(MPESA, "out", 1000.0, 6, handled_as="transfer", target=NCBA)
    bank_leg = row(NCBA, "in", 1000.0, 7, handled_as="transfer", target=MPESA)
    for legs in ([mpesa_leg, bank_leg], [bank_leg, mpesa_leg]):
        pairs = plan_pairs(legs, [mpesa_leg, bank_leg])
        assert len(pairs) == 1 and {pairs[0][0]["id"], pairs[0][1]["id"]} == {mpesa_leg["id"], bank_leg["id"]}


@pytest.mark.parametrize(
    "other",
    [
        row(MPESA, "in", 500.0, 8),  # 3 days later
        row(MPESA, "in", 499.0, 5),  # different amount
        row(MPESA, "out", 500.0, 5),  # same direction
        row(NCBA, "in", 500.0, 5),  # same account
    ],
)
def test_no_match_leaves_the_leg_waiting(other):
    leg = row(NCBA, "out", 500.0, 5, handled_as="transfer", target=MPESA)
    assert plan_pairs([leg], [leg, other]) == []


def test_closest_date_wins_then_a_waiting_leg_and_each_row_pairs_once():
    leg_a = row(NCBA, "out", 500.0, 5, handled_as="transfer", target=MPESA)
    leg_b = row(NCBA, "out", 500.0, 6, handled_as="transfer", target=MPESA)
    plain_same_day = row(MPESA, "in", 500.0, 5)
    waiting_next_day = row(MPESA, "in", 500.0, 6, handled_as="transfer", target=NCBA)
    pairs = plan_pairs([leg_a, leg_b], [plain_same_day, waiting_next_day])
    assert [(a["id"], b["id"]) for a, b in pairs] == [
        (leg_a["id"], plain_same_day["id"]),
        (leg_b["id"], waiting_next_day["id"]),
    ]


def test_a_waiting_leg_for_another_provider_is_not_claimed():
    leg = row(NCBA, "out", 500.0, 5, handled_as="transfer", target=MPESA)
    other = row(MPESA, "in", 500.0, 5, handled_as="transfer", target=EQUITY)
    assert plan_pairs([leg], [other]) == []
