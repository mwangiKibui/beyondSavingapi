"""ab-312: reading an upload with a stored profile.

fingerprint -> best active profile of the account's institution -> shared
reader -> balance check. Only a read that passes the check is returned;
anything else (no match, a failing check, a stored profile that no longer
validates, a reader bug) comes back as "not read" so the worker can fall
back to the bespoke parser. Pure and synchronous - no DB, no storage.
"""

import logging
from dataclasses import dataclass
from uuid import UUID

from app.parsers.base import ParsedTransaction
from app.parsers.profiles.fingerprint import fingerprint_pdf
from app.parsers.profiles.matching import match_profile
from app.parsers.profiles.reader import read_statement
from app.parsers.profiles.schema import validate_profile

logger = logging.getLogger(__name__)


@dataclass
class ProfileRead:
    fingerprint: str | None = None
    # Set only when the read passed its balance check.
    profile_id: UUID | None = None
    transactions: list[ParsedTransaction] | None = None
    opening_balance: float | None = None
    # The profile that matched, whether or not its read passed.
    matched_profile_id: UUID | None = None
    # Why a matched profile's read wasn't used (user-facing wording).
    failure: str | None = None
    # Another institution's layout fits this file better (warn only).
    wrong_institution: str | None = None


def read_with_profiles(
    content: bytes, *, institution_id: UUID, currency: str, candidates: list[dict]
) -> ProfileRead:
    fp = fingerprint_pdf(content)
    if fp is None:
        return ProfileRead()
    result = ProfileRead(fingerprint=fp.to_text())
    if not candidates:
        return result

    match = match_profile(fp, institution_id=institution_id, candidates=candidates)
    if match.wrong_institution is not None:
        result.wrong_institution = match.wrong_institution[1]
    if match.profile is None:
        return result

    row = match.profile
    result.matched_profile_id = row["id"]
    try:
        profile = validate_profile(row["profile"])
        read = read_statement(content, profile, default_currency=currency)
    except Exception as e:
        # A stored profile that no longer validates, a layout it doesn't fit
        # after all, or a reader bug - never fail the import over it here.
        logger.warning("Profile %s couldn't read this file: %s", row["id"], e, exc_info=True)
        result.failure = "This statement's layout couldn't be read"
        return result
    if not read.check.passed:
        result.failure = read.check.message or "The statement's balances don't add up"
        return result
    result.profile_id = row["id"]
    result.transactions = read.transactions
    result.opening_balance = read.opening_balance
    return result
