"""ab-312: matching an upload's fingerprint to an active profile.

Score = 0.5 x heading words (in order) + 0.3 x column positions + 0.2 x
labels and title. The best profile of the account's own institution at or
above MATCH_THRESHOLD wins; ties go to the most recently balance-checked,
then the most used, then the newest version. When another institution's
profile matches better, the result says so - the file was probably
uploaded to the wrong account.
"""

from dataclasses import dataclass
from difflib import SequenceMatcher
from uuid import UUID

from app.parsers.profiles.fingerprint import PageFingerprint

MATCH_THRESHOLD = 0.85
_EPOCH = ""


def similarity(a: PageFingerprint, b: PageFingerprint) -> float:
    if a.v != b.v or a.file_type != b.file_type:
        return 0.0
    if a.header or b.header:
        header = SequenceMatcher(None, a.header, b.header).ratio()
    else:
        header = 1.0
    if a.cols != b.cols:
        geometry = 0.0
    elif a.cols == 0:
        geometry = 1.0
    else:
        geometry = sum(1 for x, y in zip(a.x, b.x, strict=False) if abs(x - y) <= 1) / a.cols
    words_a, words_b = set(a.labels) | set(a.title), set(b.labels) | set(b.title)
    labels = 1.0 if not words_a and not words_b else len(words_a & words_b) / len(words_a | words_b)
    return round(0.5 * header + 0.3 * geometry + 0.2 * labels, 4)


@dataclass
class MatchResult:
    profile: dict | None
    score: float
    # (institution_id, institution name, score) of a better match elsewhere.
    wrong_institution: tuple[UUID, str, float] | None


def _rank(row: dict, score: float):
    checked = row.get("last_check_passed_at")
    return (score, checked.isoformat() if checked else _EPOCH, row.get("usage_count") or 0, row.get("version") or 0)


def match_profile(fp: PageFingerprint, *, institution_id: UUID, candidates: list[dict]) -> MatchResult:
    """candidates: active profile rows (id, institution_id, institution_name,
    fingerprint text, profile, version, usage_count, last_check_passed_at)."""
    best_own, best_own_rank = None, None
    best_other, best_other_score = None, 0.0
    for row in candidates:
        try:
            score = similarity(fp, PageFingerprint.from_text(row["fingerprint"]))
        except Exception:
            continue
        if row["institution_id"] == institution_id:
            rank = _rank(row, score)
            if score >= MATCH_THRESHOLD and (best_own_rank is None or rank > best_own_rank):
                best_own, best_own_rank = row, rank
        elif score >= MATCH_THRESHOLD and score > best_other_score:
            best_other, best_other_score = row, score
    own_score = best_own_rank[0] if best_own_rank else 0.0
    wrong = None
    if best_other is not None and best_other_score > own_score:
        wrong = (best_other["institution_id"], best_other["institution_name"], best_other_score)
    return MatchResult(profile=best_own, score=own_score, wrong_institution=wrong)
