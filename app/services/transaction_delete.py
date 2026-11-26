"""Delete a manual transaction, and undo that (ab-342).

Only rows the user typed in can be deleted: a statement row comes from the
bank, so deleting it would break the account's running balance and come
back with the next overlapping upload - it is re-filed (ab-340) or its
upload is deleted instead. Deleting is a soft delete."""

from uuid import UUID

import asyncpg

from app.repositories.transactions import (
    lock_deleted_manual_transaction,
    lock_transaction_for_delete,
    restore_transaction,
    soft_delete_transaction,
)
from app.services.allocations import AllocationRejected
from app.services.import_handling import pair_hand_filed_transfers, undo_handling, unpair_hand_filed

STATEMENT_ROW = (
    "Rows from a statement can't be deleted. Change how they're allocated, or delete the upload."
)


async def delete_manual_transaction(
    conn: asyncpg.Connection, *, user_id: UUID, transaction_id: UUID, secret: str
) -> list[UUID]:
    """Returns the other sides of a transfer this row was paired with, now
    back in their review."""
    row = await lock_transaction_for_delete(conn, transaction_id=transaction_id, user_id=user_id)
    if row is None:
        raise AllocationRejected(404, "Transaction not found")
    if row["import_id"] is not None:
        raise AllocationRejected(409, STATEMENT_ROW)
    released: list[UUID] = []
    # A paired transfer is unpaired first, as Undo / Edit do: the other side
    # goes back to waiting or the review.
    if row["handled_as"]:
        back = await undo_handling(conn, transaction_id=transaction_id, user_id=user_id, secret=secret)
        released = [t for t in back if t != transaction_id]
    elif row["transfer_group_id"] is not None:
        released = await unpair_hand_filed(conn, user_id=user_id, transaction_ids=[transaction_id], secret=secret)
    await soft_delete_transaction(conn, transaction_id=transaction_id)
    return released


async def restore_manual_transaction(conn: asyncpg.Connection, *, user_id: UUID, transaction_id: UUID) -> None:
    """Undo a delete: the row and its allocations count again; a Funds
    transfer to one of the user's accounts pairs again."""
    row = await lock_deleted_manual_transaction(conn, transaction_id=transaction_id, user_id=user_id)
    if row is None:
        raise AllocationRejected(404, "Transaction not found")
    try:
        await restore_transaction(conn, transaction_id=transaction_id)
    except asyncpg.UniqueViolationError:
        # The same entry was added again since: restoring would duplicate it.
        raise AllocationRejected(409, "This transaction has been added again since it was deleted") from None
    await pair_hand_filed_transfers(conn, user_id=user_id)
