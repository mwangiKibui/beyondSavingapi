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
from app.parsers.profiles.builtin import load_builtin_profiles
from app.parsers.profiles.fingerprint import fingerprint_file
from app.parsers.profiles.matching import match_profile
from app.parsers.profiles.reader import read_statement
from app.parsers.profiles.schema import validate_profile
from app.repositories.statement_profiles import (
    create_profile,
    find_institution_id,
    list_active_builtin_profiles,
    retire_profiles,
)

logger = logging.getLogger(__name__)


@dataclass
class ProfileRead:
    fingerprint: str | None = None
    # Set only when the read passed its balance check.
    profile_id: UUID | None = None
    profile_version: int | None = None
    transactions: list[ParsedTransaction] | None = None
    opening_balance: float | None = None
    # The profile that matched, whether or not its read passed.
    matched_profile_id: UUID | None = None
    # Why a matched profile's read wasn't used (user-facing wording).
    failure: str | None = None
    # Another institution's layout fits this file better (warn only).
    wrong_institution: str | None = None


def read_with_profiles(
    content: bytes,
    *,
    institution_id: UUID,
    currency: str,
    candidates: list[dict],
    file_name: str = "statement.pdf",
) -> ProfileRead:
    fp = fingerprint_file(content, file_name)
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

    return _read_into(result, content, match.profile, currency=currency)


def read_with_chosen_profile(content: bytes, *, row: dict, currency: str) -> ProfileRead:
    """The upload read with the layout its uploader just saved for it - no
    fingerprint matching, which could miss it once the reader changes how
    it fingerprints a page."""
    return _read_into(ProfileRead(), content, row, currency=currency)


def _read_into(result: ProfileRead, content: bytes, row: dict, *, currency: str) -> ProfileRead:
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
    result.profile_version = row.get("version")
    result.transactions = read.transactions
    result.opening_balance = read.opening_balance
    return result


# Any fixed number: serialises API instances seeding at the same time.
_BUILTIN_LOCK = 3130001


async def ensure_builtin_profiles(pool) -> None:
    """ab-313: makes each shipped profile (app/parsers/profiles/builtin)
    the active built-in profile of its institution. Unchanged ones are left
    alone; a changed one is stored as the next version and the previous
    built-in retired. Never raises - startup carries on without it."""
    for builtin in load_builtin_profiles():
        try:
            clean = validate_profile(builtin.profile).model_dump(mode="json")
        except Exception:
            logger.error("Built-in profile %s is invalid - skipped", builtin.key, exc_info=True)
            continue
        try:
            async with pool.acquire() as conn, conn.transaction():
                await conn.execute("SELECT pg_advisory_xact_lock($1)", _BUILTIN_LOCK)
                institution_id = await find_institution_id(
                    conn, name=builtin.institution_name, type=builtin.institution_type
                )
                if institution_id is None:
                    logger.warning(
                        "Built-in profile %s: no active institution %s (%s) - skipped",
                        builtin.key,
                        builtin.institution_name,
                        builtin.institution_type,
                    )
                    continue
                current = await list_active_builtin_profiles(conn, institution_id=institution_id)
                if any(r["fingerprint"] == builtin.fingerprint and r["profile"] == clean for r in current):
                    continue
                created = await create_profile(
                    conn,
                    institution_id=institution_id,
                    fingerprint=builtin.fingerprint,
                    profile=builtin.profile,
                    source="builtin",
                    status="active",
                )
                if current:
                    await retire_profiles(conn, profile_ids=[r["id"] for r in current])
                logger.info("Built-in profile %s stored as version %s", builtin.key, created["version"])
        except Exception:
            logger.error("Storing built-in profile %s failed", builtin.key, exc_info=True)
