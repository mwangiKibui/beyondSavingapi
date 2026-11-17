"""Handles the rows of an import that need no decision (ab-281).

Runs on ONE connection the caller has opened a transaction on. Each
import is handled once (statement_imports.handled_at): by the worker
right after parsing, or - for imports parsed before ab-281 - the first
time their review opens. Rows the user already categorised, rows they
un-handled before (handling_dismissed) and paired transfers are left as
they are. Nothing here learns rules or votes, so nothing about a row -
a person's in particular - is shared.
"""

from collections import Counter
from dataclasses import dataclass, field
from datetime import timedelta
from uuid import UUID, uuid4

import asyncpg

from app.repositories.category_suggestions import create_categories, get_import_rows
from app.repositories.row_handling import (
    delete_allocations,
    dismiss_handling,
    get_category,
    get_handled_transaction,
    get_import_to_handle,
    get_or_create_transfer_reason,
    get_owned_providers,
    get_pair_candidates,
    get_waiting_transfer_legs,
    insert_handled_allocations,
    link_transfer_pair,
    lock_import,
    mark_import_handled,
    set_handled,
    set_transfer_group,
    unlink_transfer,
)
from app.services.allocations import CATEGORY_TYPE_FOR_DIRECTION
from app.services.merchant_keys import extract_key
from app.services.row_handling import (
    BETWEEN_MY_ACCOUNTS,
    PAIR_WINDOW_DAYS,
    Handling,
    classify_row,
    plan_pairs,
    plan_saving_pairs,
)


class HandlingRejected(Exception):
    def __init__(self, status_code: int, detail: str):
        super().__init__(detail)
        self.status_code = status_code
        self.detail = detail


@dataclass
class HandleResult:
    counts: Counter = field(default_factory=Counter)
    paired: int = 0


def _classify(row: dict, *, provider: str, owned: frozenset[str], secret: str) -> Handling | None:
    key = extract_key(provider=provider, description=row["description"], direction=row["direction"], secret=secret)
    return classify_row(
        provider=provider,
        description=row["description"],
        direction=row["direction"],
        key=key,
        owned_providers=owned,
        sub_ledger_role=row.get("sub_ledger_role"),
    )


def _is_untouched(row: dict) -> bool:
    return (
        row["handled_as"] is None
        and not row["handling_dismissed"]
        and not row["is_transfer"]
        and float(row["allocated"] or 0) == 0
    )


def _waiting_allocation(row: dict, *, reason_id: UUID, handling: Handling) -> dict:
    return {
        "transaction_id": row["id"],
        "transfer_reason_id": reason_id,
        "source_description": handling.target_provider,
        "amount": float(row["amount"]),
        "currency": row["currency"],
    }


def _sacco_move(row: dict, handling: Handling, savings_side_of: dict, spending_side_of: dict) -> Handling:
    """The reason each side of a paired move inside a SACCO shows."""
    if row["id"] in savings_side_of:
        name = savings_side_of[row["id"]]["sub_ledger_name"]
        reason = f"Moved to your {name}" if row["direction"] == "out" else f"Moved from your {name}"
        return Handling(kind="transfer", reason=reason)
    if row["id"] in spending_side_of:
        name = spending_side_of[row["id"]]["sub_ledger_name"]
        reason = f"Into your savings from {name}" if row["direction"] == "in" else f"Out of your savings to {name}"
        return Handling(kind="saving", reason=reason)
    return handling


async def handle_import(conn: asyncpg.Connection, *, statement_import: dict, secret: str) -> HandleResult | None:
    """statement_import: id, account_id, provider, user_id, period_start,
    period_end. Returns None when the import was already handled."""
    locked = await lock_import(conn, import_id=statement_import["id"])
    if locked is None or locked["handled_at"] is not None:
        return None
    user_id, provider = statement_import["user_id"], statement_import["provider"]
    rows = await get_import_rows(
        conn,
        import_id=statement_import["id"],
        account_id=statement_import["account_id"],
        period_start=statement_import["period_start"],
        period_end=statement_import["period_end"],
    )
    owned = frozenset(await get_owned_providers(conn, user_id=user_id))
    planned = [
        (row, handling)
        for row in rows
        if _is_untouched(row) and (handling := _classify(row, provider=provider, owned=owned, secret=secret))
    ]
    # ab-259: a move inside a SACCO shows on both sub-ledgers. Saving is
    # counted on the savings side; the withdrawable side is a transfer.
    sacco_pairs = plan_saving_pairs([row for row, h in planned if h.kind == "saving"])
    savings_side_of = {spending["id"]: savings for spending, savings in sacco_pairs}
    spending_side_of = {savings["id"]: spending for spending, savings in sacco_pairs}
    planned = [(row, _sacco_move(row, h, savings_side_of, spending_side_of)) for row, h in planned]

    result = HandleResult(counts=Counter(h.kind for _, h in planned))
    if planned:
        names = sorted({h.category for _, h in planned if h.category})
        categories = await create_categories(conn, user_id=user_id, categories=[(n, "expense") for n in names])
        category_ids = {c["name"].lower(): c["id"] for c in categories}
        reason_id = (
            await get_or_create_transfer_reason(conn, user_id=user_id, name=BETWEEN_MY_ACCOUNTS)
            if result.counts["transfer"]
            else None
        )
        await set_handled(
            conn,
            transaction_ids=[row["id"] for row, _ in planned],
            kinds=[h.kind for _, h in planned],
            reasons=[h.reason for _, h in planned],
        )
        allocations = []
        for row, h in planned:
            if float(row["amount"]) <= 0:
                continue  # e.g. a loan request: nothing to reconcile
            if h.kind == "transfer" and row["id"] in savings_side_of:
                allocations.append({
                    "transaction_id": row["id"],
                    "transfer_reason_id": reason_id,
                    "source_account_id": row["account_id"],
                    "amount": float(row["amount"]),
                    "currency": row["currency"],
                })
            elif h.kind == "transfer":
                allocations.append(_waiting_allocation(row, reason_id=reason_id, handling=h))
            else:
                allocations.append({
                    "transaction_id": row["id"],
                    "category_id": category_ids[h.category.lower()] if h.category else None,
                    "amount": float(row["amount"]),
                    "currency": row["currency"],
                })
        await insert_handled_allocations(conn, allocations=allocations)
        for spending, savings in sacco_pairs:
            await set_transfer_group(conn, group_id=uuid4(), ids=[spending["id"], savings["id"]])

    result.paired = await pair_transfers(conn, user_id=user_id, secret=secret, owned=owned)
    await mark_import_handled(conn, import_id=statement_import["id"])
    return result


async def pair_transfers(
    conn: asyncpg.Connection, *, user_id: UUID, secret: str, owned: frozenset[str] | None = None
) -> int:
    """Pairs every waiting transfer leg of the user's that now has its
    other leg (either upload order). Returns how many pairs were made."""
    legs = await get_waiting_transfer_legs(conn, user_id=user_id)
    if not legs:
        return 0
    if owned is None:
        owned = frozenset(await get_owned_providers(conn, user_id=user_id))

    def with_target(row: dict) -> dict:
        handling = _classify(row, provider=row["provider"], owned=owned, secret=secret)
        target = handling.target_provider if handling and handling.kind == "transfer" else None
        return {**row, "target_provider": target}

    legs = [leg for leg in map(with_target, legs) if leg["target_provider"]]
    if not legs:
        return 0
    window = timedelta(days=PAIR_WINDOW_DAYS)
    candidates = await get_pair_candidates(
        conn,
        user_id=user_id,
        amounts=sorted({float(leg["amount"]) for leg in legs}),
        from_date=min(leg["txn_date"] for leg in legs) - window,
        to_date=max(leg["txn_date"] for leg in legs) + window,
    )
    candidates = [with_target(c) if c["handled_as"] == "transfer" else c for c in candidates]
    pairs = plan_pairs(legs, candidates)
    if not pairs:
        return 0

    reason_id = await get_or_create_transfer_reason(conn, user_id=user_id, name=BETWEEN_MY_ACCOUNTS)
    for leg, other in pairs:
        out_leg, in_leg = (leg, other) if leg["direction"] == "out" else (other, leg)
        await link_transfer_pair(
            conn,
            group_id=uuid4(),
            ids=[out_leg["id"], in_leg["id"]],
            reasons=[f"Moved to your {in_leg['nickname']} account", f"Moved from your {out_leg['nickname']} account"],
        )
        await delete_allocations(conn, transaction_ids=[out_leg["id"], in_leg["id"]])
        await insert_handled_allocations(
            conn,
            allocations=[
                {
                    "transaction_id": this["id"],
                    "transfer_reason_id": reason_id,
                    "source_account_id": that["account_id"],
                    "amount": float(this["amount"]),
                    "currency": this["currency"],
                }
                for this, that in ((out_leg, in_leg), (in_leg, out_leg))
                if float(this["amount"]) > 0
            ],
        )
    return len(pairs)


async def undo_handling(conn: asyncpg.Connection, *, transaction_id: UUID, user_id: UUID, secret: str) -> list[UUID]:
    """"Not a charge / loan / transfer": the row goes back to the review
    and is never handled or paired again. A paired transfer's other leg
    goes back to waiting when it is a transfer by itself (e.g. "TRF TO
    M-PESA"), else back to the review too. Returns the rows back in review."""
    row = await get_handled_transaction(conn, transaction_id=transaction_id, user_id=user_id)
    if row is None:
        raise HandlingRejected(404, "Transaction not found")
    if row["handled_as"] is None:
        raise HandlingRejected(409, "This row wasn't handled automatically")
    await delete_allocations(conn, transaction_ids=[row["id"]])
    await dismiss_handling(conn, transaction_id=row["id"])
    back_in_review = [row["id"]]

    other_id = row["paired_transaction_id"]
    if other_id is not None:
        other = await get_handled_transaction(conn, transaction_id=other_id, user_id=user_id)
        await unlink_transfer(conn, transaction_ids=[other_id])
        await delete_allocations(conn, transaction_ids=[other_id])
        owned = frozenset(await get_owned_providers(conn, user_id=user_id))
        handling = _classify(other, provider=other["provider"], owned=owned, secret=secret)
        if handling and handling.kind == "saving":
            # ab-259: the other side of a move inside a SACCO stays saving.
            await set_handled(conn, transaction_ids=[other_id], kinds=["saving"], reasons=[handling.reason])
            if float(other["amount"]) > 0:
                await insert_handled_allocations(
                    conn,
                    allocations=[{
                        "transaction_id": other_id,
                        "amount": float(other["amount"]),
                        "currency": other["currency"],
                    }],
                )
        elif handling and handling.kind == "transfer":
            await set_handled(conn, transaction_ids=[other_id], kinds=["transfer"], reasons=[handling.reason])
            if float(other["amount"]) > 0:
                reason_id = await get_or_create_transfer_reason(conn, user_id=user_id, name=BETWEEN_MY_ACCOUNTS)
                await insert_handled_allocations(
                    conn, allocations=[_waiting_allocation(other, reason_id=reason_id, handling=handling)]
                )
            await pair_transfers(conn, user_id=user_id, secret=secret, owned=owned)
        else:
            await set_handled(conn, transaction_ids=[other_id], kinds=[None], reasons=[None])
            back_in_review.append(other_id)
    return back_in_review


async def change_handled_category(
    conn: asyncpg.Connection, *, transaction_id: UUID, user_id: UUID, category_id: UUID
) -> dict:
    """Files a handled charge or loan row under another of the user's
    categories. The row stays handled."""
    row = await get_handled_transaction(conn, transaction_id=transaction_id, user_id=user_id)
    if row is None:
        raise HandlingRejected(404, "Transaction not found")
    if row["handled_as"] not in ("charge", "loan"):
        raise HandlingRejected(409, "Only a handled charge or loan row can be given another category")
    category = await get_category(conn, category_id=category_id, user_id=user_id)
    if category is None:
        raise HandlingRejected(404, "Category not found")
    if category["type"] != CATEGORY_TYPE_FOR_DIRECTION[row["direction"]]:
        raise HandlingRejected(422, "The category's type doesn't match the row's direction")
    if float(row["amount"]) <= 0:
        raise HandlingRejected(422, "A zero-amount row can't be categorised")
    await delete_allocations(conn, transaction_ids=[row["id"]])
    await insert_handled_allocations(
        conn,
        allocations=[{
            "transaction_id": row["id"],
            "category_id": category["id"],
            "amount": float(row["amount"]),
            "currency": row["currency"],
        }],
    )
    return {"transaction_id": row["id"], "category_id": category["id"], "category_name": category["name"]}


async def handle_import_by_id(pool: asyncpg.Pool, *, import_id: UUID, secret: str) -> HandleResult | None:
    """Runs handle_import in its own transaction for a parsed import that
    hasn't been reviewed or handled yet. Reviewed imports are left as the
    user finished them."""
    async with pool.acquire() as conn:
        async with conn.transaction():
            statement_import = await get_import_to_handle(conn, import_id=import_id)
            if (
                statement_import is None
                or statement_import["status"] != "parsed"
                or statement_import["reviewed_at"] is not None
                or statement_import["handled_at"] is not None
            ):
                return None
            return await handle_import(conn, statement_import=statement_import, secret=secret)
