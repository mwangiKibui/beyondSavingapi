from pathlib import Path

import pytest

from app.parsers.mentor_sacco import parse_mentor_sacco_statement

FIXTURE = Path(__file__).parent / "fixtures" / "statements" / "mentor_sacco" / "member_statement_sample.pdf"


@pytest.fixture(scope="module")
def content() -> bytes:
    return FIXTURE.read_bytes()


@pytest.fixture(scope="module")
def sections(content):
    return parse_mentor_sacco_statement(content)


def by_name(sections, name, index=0):
    matches = [s for s in sections if s["name"] == name]
    return matches[index]


def test_returns_one_entry_per_section_instance_including_duplicate_names(sections):
    # 5 section instances: Ordinary Deposit, Savings Account, Share
    # Capital, and "Instant Loan" appearing twice (a fresh disbursement
    # and a separate pre-existing amortization ledger) - not merged.
    assert len(sections) == 5
    assert [s["name"] for s in sections] == [
        "Ordinary Deposit",
        "Savings Account",
        "Share Capital",
        "Instant Loan",
        "Instant Loan",
    ]


def test_ordinary_deposit_uses_its_explicit_opening_balance(sections):
    section = by_name(sections, "Ordinary Deposit")
    assert section["opening_balance"] == 231850.0
    assert len(section["transactions"]) == 2
    assert section["transactions"][0]["direction"] == "in"
    assert section["transactions"][-1]["balance_after"] == 235679.75


def test_share_capital_has_no_transactions_just_an_opening_balance(sections):
    # Only an "Opening Balance" row exists for this sub-ledger in the
    # sample - correctly not counted as a transaction.
    section = by_name(sections, "Share Capital")
    assert section["opening_balance"] == 30000.0
    assert section["transactions"] == []


def test_savings_account_derives_an_implicit_opening_balance(sections):
    # No stated "Opening Balance" line for this sub-ledger - backed out
    # from its first row (a 100.00 deposit landing on a stated balance
    # of 0.00, implying an opening of -100.00) rather than assuming 0.
    section = by_name(sections, "Savings Account")
    assert section["opening_balance"] == -100.0
    assert section["transactions"][0]["amount"] == 100.0
    assert section["transactions"][0]["direction"] == "in"
    assert section["transactions"][0]["balance_after"] == 0.0
    assert len(section["transactions"]) == 29
    assert section["transactions"][-1]["balance_after"] == 250592.0


def test_returns_oldest_first_within_each_section(sections):
    for section in sections:
        dates = [t["txn_date"] for t in section["transactions"]]
        assert dates == sorted(dates)


def test_instant_loan_disbursement_is_a_separate_instance_from_the_amortization_ledger(sections):
    disbursement = by_name(sections, "Instant Loan", index=0)
    amortization = by_name(sections, "Instant Loan", index=1)

    # The disbursement has no stated opening (a brand-new loan) - backed
    # out to 0, and its single transaction is the disbursement itself.
    assert disbursement["opening_balance"] == 0.0
    assert len(disbursement["transactions"]) == 1
    assert disbursement["transactions"][0]["amount"] == 716000.0
    assert disbursement["transactions"][0]["direction"] == "in"

    # The amortization ledger is a separate, pre-existing balance
    # (explicit opening from before the statement period) paid down to
    # exactly zero by the end of the period.
    assert amortization["opening_balance"] == 612505.25
    assert amortization["transactions"][-1]["balance_after"] == 0.0
    assert len(amortization["transactions"]) == 9


def test_every_transaction_is_kes(sections):
    for section in sections:
        assert all(t["currency"] == "KES" for t in section["transactions"])


def test_counterparty_is_not_mapped(sections):
    for section in sections:
        assert all(t["counterparty"] is None for t in section["transactions"])


def test_same_document_no_across_sub_ledgers_gets_different_dedupe_hashes(sections):
    # Document No ATRC-07315 posts to Ordinary Deposit, Savings Account
    # (twice), and the Instant Loan amortization ledger (twice) - one
    # real-world event fanning out across sub-ledgers, per the runbook.
    # Two of these even share identical description text and amount
    # ("MPA Recovery...", 3000.0) but land in different sub-ledgers -
    # all five must still hash differently.
    matches = [
        t
        for section in sections
        for t in section["transactions"]
        if "ATRC-07315" in t["description"]
    ]
    assert len(matches) == 5
    assert len({t["dedupe_hash"] for t in matches}) == 5


def test_dedupe_hash_is_stable_and_unique_within_each_section(sections):
    for section in sections:
        hashes = [t["dedupe_hash"] for t in section["transactions"]]
        assert len(hashes) == len(set(hashes))
        assert all(len(h) == 64 for h in hashes)


def test_parsing_is_deterministic(content):
    first_run = parse_mentor_sacco_statement(content)
    second_run = parse_mentor_sacco_statement(content)
    assert first_run == second_run
