"""ab-312: fingerprint -> match -> read -> balance check, as one decision."""

from uuid import uuid4

from app.parsers.profiles.fingerprint import fingerprint_pdf
from app.services.statement_profiles import read_with_profiles
from tests.statement_pdfs import Layout, build, sample_rows

OWN, OTHER = uuid4(), uuid4()
PROFILE = {
    "date_format": "%d/%m/%Y",
    "table": {
        "columns": [
            {"role": "date", "header": "Date"},
            {"role": "description", "header": "Details"},
            {"role": "money_in", "header": "Money In"},
            {"role": "money_out", "header": "Money Out"},
            {"role": "balance", "header": "Balance"},
        ],
        "column_bounds": "header_gaps",
        "end_markers": ["End of statement"],
    },
}
GOOD = build(sample_rows(6))


def candidate(content=GOOD, institution=OWN, *, name="Own Bank", profile=PROFILE):
    return {
        "id": uuid4(),
        "institution_id": institution,
        "institution_name": name,
        "fingerprint": fingerprint_pdf(content).to_text(),
        "profile": profile,
        "version": 1,
        "usage_count": 0,
        "last_check_passed_at": None,
    }


def test_a_matching_profile_that_passes_the_check_reads_the_file():
    c = candidate()
    result = read_with_profiles(GOOD, institution_id=OWN, currency="KES", candidates=[c])
    assert result.profile_id == c["id"]
    assert len(result.transactions) == 6
    assert result.failure is None
    assert result.fingerprint == c["fingerprint"]


def test_no_candidates_still_gives_the_fingerprint():
    result = read_with_profiles(GOOD, institution_id=OWN, currency="KES", candidates=[])
    assert result.transactions is None and result.profile_id is None
    assert result.fingerprint == fingerprint_pdf(GOOD).to_text()


def test_a_matched_profile_whose_check_fails_reports_why():
    bad = build([*sample_rows(3), ["05/10/2026", "Odd", "", "10.00", "5.00"]])
    result = read_with_profiles(bad, institution_id=OWN, currency="KES", candidates=[candidate()])
    assert result.transactions is None
    assert result.matched_profile_id is not None
    assert "balance" in result.failure.lower()


def test_another_institutions_profile_is_never_used():
    result = read_with_profiles(GOOD, institution_id=OWN, currency="KES", candidates=[candidate(institution=OTHER)])
    assert result.transactions is None
    assert result.wrong_institution == "Own Bank"


def test_a_profile_that_no_longer_validates_is_skipped():
    broken = candidate(profile={"date_format": "%d"})
    result = read_with_profiles(GOOD, institution_id=OWN, currency="KES", candidates=[broken])
    assert result.transactions is None and result.failure is not None


def test_a_file_that_isnt_a_pdf_has_no_fingerprint_and_no_read():
    result = read_with_profiles(b"a,b\n", institution_id=OWN, currency="KES", candidates=[candidate()])
    assert result.fingerprint is None and result.transactions is None and result.failure is None


def test_a_reader_crash_is_a_failure_not_an_exception(monkeypatch):
    def boom(*a, **k):
        raise RuntimeError("bug")

    monkeypatch.setattr("app.services.statement_profiles.read_statement", boom)
    result = read_with_profiles(GOOD, institution_id=OWN, currency="KES", candidates=[candidate()])
    assert result.transactions is None and result.failure is not None


def test_another_layout_of_the_same_bank_doesnt_match():
    other = build(sample_rows(4), Layout(headers=["Posted", "Narrative", "Credit", "Debit", "Running"]))
    result = read_with_profiles(other, institution_id=OWN, currency="KES", candidates=[candidate()])
    assert result.matched_profile_id is None and result.transactions is None
