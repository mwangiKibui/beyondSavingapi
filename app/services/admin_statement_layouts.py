"""ab-317: an admin retires, restores and edits statement layouts.

Exactly one active version per layout (institution + fingerprint): matching
prefers a profile that passed a check, so a fresh version would otherwise
lose to the one it replaces. An edit is stored as the next version
(source 'admin') and takes effect at once - each new file is still
balance-checked when it is read. Uploads waiting for support are re-read
whenever a layout becomes active.
"""

import logging
from uuid import UUID

from app.parsers.profiles.schema import InvalidStatementProfile, validate_profile
from app.repositories.statement_profiles import (
    create_profile,
    get_profile_for_update,
    retire_other_active,
    set_profile_status,
)
from app.services.statement_layouts import requeue_waiting

logger = logging.getLogger(__name__)


class LayoutAdminError(Exception):
    def __init__(self, status_code: int, detail: str):
        super().__init__(detail)
        self.status_code, self.detail = status_code, detail


NOT_FOUND = "Statement layout not found"


async def _locked(conn, profile_id: UUID) -> dict:
    row = await get_profile_for_update(conn, profile_id=profile_id)
    if row is None:
        raise LayoutAdminError(404, NOT_FOUND)
    return row


async def _reread(pool, institution_id: UUID) -> None:
    """Never undoes the change: a waiting upload is re-read on the next change anyway."""
    try:
        await requeue_waiting(pool, institution_id=institution_id)
    except Exception:
        logger.error("Re-reading uploads waiting for support failed", exc_info=True)


async def retire_layout(pool, *, profile_id: UUID) -> None:
    async with pool.acquire() as conn, conn.transaction():
        await _locked(conn, profile_id)
        await set_profile_status(conn, profile_id=profile_id, status="retired")


async def restore_layout(pool, *, profile_id: UUID) -> None:
    async with pool.acquire() as conn, conn.transaction():
        row = await _locked(conn, profile_id)
        await set_profile_status(conn, profile_id=profile_id, status="active")
        await retire_other_active(
            conn, institution_id=row["institution_id"], fingerprint=row["fingerprint"], keep_id=profile_id
        )
    await _reread(pool, row["institution_id"])


async def edit_layout(pool, *, profile_id: UUID, profile: dict, admin_id: UUID) -> dict:
    try:
        clean = validate_profile(profile).model_dump(mode="json")
    except InvalidStatementProfile as e:
        raise LayoutAdminError(422, str(e)) from None
    async with pool.acquire() as conn, conn.transaction():
        row = await _locked(conn, profile_id)
        created = await create_profile(
            conn,
            institution_id=row["institution_id"],
            fingerprint=row["fingerprint"],
            profile=clean,
            source="admin",
            status="active",
            created_by=admin_id,
        )
        await retire_other_active(
            conn, institution_id=row["institution_id"], fingerprint=row["fingerprint"], keep_id=created["id"]
        )
    await _reread(pool, row["institution_id"])
    return created
