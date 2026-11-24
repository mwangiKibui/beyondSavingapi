"""ab-312: picking the profile for an upload from its fingerprint."""

from datetime import UTC, datetime
from uuid import uuid4

from app.parsers.profiles.fingerprint import PageFingerprint
from app.parsers.profiles.matching import MATCH_THRESHOLD, match_profile, similarity

FP = PageFingerprint(
    title=["account statement"],
    labels=["account name", "statement period"],
    header=["date", "details", "money in", "money out", "balance"],
    cols=5,
    x=[3, 7, 22, 27, 33],
)
OWN, OTHER = uuid4(), uuid4()


def row(fp: PageFingerprint, institution=OWN, *, name="Own Bank", checked=None, usage=0, version=1):
    return {
        "id": uuid4(),
        "institution_id": institution,
        "institution_name": name,
        "fingerprint": fp.to_text(),
        "profile": {},
        "version": version,
        "usage_count": usage,
        "last_check_passed_at": checked,
    }


def test_an_identical_fingerprint_scores_one():
    assert similarity(FP, FP) == 1.0


def test_a_little_drift_in_column_positions_still_matches():
    drifted = FP.model_copy(update={"x": [3, 8, 22, 26, 33]})
    assert similarity(FP, drifted) >= MATCH_THRESHOLD


def test_a_different_column_count_doesnt_match():
    other = FP.model_copy(update={"header": FP.header[:4], "cols": 4, "x": FP.x[:4]})
    assert similarity(FP, other) < MATCH_THRESHOLD


def test_a_different_file_type_never_matches():
    assert similarity(FP, FP.model_copy(update={"file_type": "csv"})) == 0.0


def test_the_best_own_profile_wins():
    weak = FP.model_copy(update={"labels": ["something else"], "title": []})
    best, other = row(FP), row(weak)
    result = match_profile(FP, institution_id=OWN, candidates=[other, best])
    assert result.profile is best and result.wrong_institution is None


def test_ties_go_to_the_most_recently_checked_then_most_used():
    older = row(FP, checked=datetime(2026, 10, 1, tzinfo=UTC), usage=50)
    newer = row(FP, checked=datetime(2026, 11, 1, tzinfo=UTC), usage=1)
    assert match_profile(FP, institution_id=OWN, candidates=[older, newer]).profile is newer
    busy, quiet = row(FP, usage=9), row(FP, usage=2)
    assert match_profile(FP, institution_id=OWN, candidates=[quiet, busy]).profile is busy


def test_no_match_below_the_threshold():
    other = FP.model_copy(update={"header": ["a", "b", "c"], "cols": 3, "x": [1, 2, 3], "labels": [], "title": []})
    result = match_profile(FP, institution_id=OWN, candidates=[row(other)])
    assert result.profile is None


def test_a_file_that_matches_another_bank_better_is_flagged():
    mine = FP.model_copy(update={"labels": ["other label"], "title": []})
    result = match_profile(
        FP, institution_id=OWN, candidates=[row(mine), row(FP, OTHER, name="Other Bank")]
    )
    assert result.wrong_institution == (OTHER, "Other Bank", 1.0)


def test_a_weaker_match_at_another_bank_is_not_flagged():
    weaker = FP.model_copy(update={"labels": ["other label"], "title": []})
    result = match_profile(FP, institution_id=OWN, candidates=[row(FP), row(weaker, OTHER, name="Other Bank")])
    assert result.profile is not None and result.wrong_institution is None


def test_no_candidates():
    result = match_profile(FP, institution_id=OWN, candidates=[])
    assert result.profile is None and result.wrong_institution is None
