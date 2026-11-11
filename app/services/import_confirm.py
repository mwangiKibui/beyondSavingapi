"""Confirm step for a statement import's category preview (ab-253).

Runs on ONE connection the caller has opened a transaction on: creates
the new categories the accepted suggestions need, allocates each row,
learns the user's rules, records shared-list and unknown-number votes,
and marks the import reviewed. Any rejection or error rolls all of it
back. Budget alerts are the caller's job, after commit.
"""

from dataclasses import dataclass, field
from uuid import UUID

import asyncpg

from app.repositories.category_suggestions import (
    add_users_number_keys,
    create_categories,
    get_approved_directory,
    get_blocked_numbers,
    get_import_rows,
    get_learned_names,
    get_user_categories,
    lock_transactions,
    mark_import_reviewed,
    record_name_sightings,
    set_rule_labels,
    upsert_merchant_votes,
    upsert_number_votes,
    upsert_rules,
)
from app.repositories.import_progress import delete_progress
from app.repositories.transactions import insert_bulk_allocations
from app.services.allocations import (
    CATEGORY_TYPE_FOR_DIRECTION,
    AllocationPlan,
    AllocationRejected,
    plan_bulk_allocation,
)
from app.services.category_suggestions import (
    Directory,
    apply_learned_names,
    extract_keys,
    learned_name_lookups,
    name_sightings,
    plan_key_growth,
)
from app.services.suggestion_learning import ConfirmedRow, plan_learning


@dataclass(frozen=True)
class ConfirmItem:
    transaction_id: UUID
    category_id: UUID | None = None
    new_category_name: str | None = None
    label: str | None = None
    user_entered_paybill: str | None = None


@dataclass
class ConfirmResult:
    allocated: list[UUID]
    skipped: list[UUID]
    created_categories: list[dict]
    learned: int
    reviewed_at: object
    plan: AllocationPlan = field(repr=False)
    transactions_by_id: dict = field(repr=False)


def _name_key(name: str, category_type: str) -> tuple[str, str]:
    return (name.strip().lower(), category_type)


async def confirm_import(
    conn: asyncpg.Connection,
    *,
    user_id: UUID,
    statement_import: dict,
    items: list[ConfirmItem],
    secret: str,
) -> ConfirmResult:
    item_by_id = {item.transaction_id: item for item in items}
    await lock_transactions(conn, transaction_ids=list(item_by_id))

    # Same scope as the preview: this import's rows plus statement rows on
    # its account within its period.
    rows = await get_import_rows(
        conn,
        import_id=statement_import["id"],
        account_id=statement_import["account_id"],
        period_start=statement_import["period_start"],
        period_end=statement_import["period_end"],
    )
    rows_by_id = {r["id"]: r for r in rows}
    if item_by_id.keys() - rows_by_id.keys():
        raise AllocationRejected(422, "One or more rows aren't part of this statement")

    directory = Directory.from_rows(await get_approved_directory(conn), blocked=await get_blocked_numbers(conn))
    printed = extract_keys(rows, provider=statement_import["provider"], secret=secret)
    for transaction_id, item in item_by_id.items():
        if item.user_entered_paybill and printed[transaction_id].kind != "reference":
            raise AllocationRejected(422, "A Paybill can only be added to a row that shows an account reference")

    categories = await get_user_categories(conn, user_id=user_id)
    by_id = {c["id"]: c for c in categories}
    by_name = {_name_key(c["name"], c["type"]): c for c in categories}

    # New categories: reuse the user's own by name (ignoring case), and
    # create each missing (name, type) once however many rows ask for it.
    to_create: dict[tuple[str, str], str] = {}
    for transaction_id, item in item_by_id.items():
        if item.new_category_name is None:
            continue
        name_key = _name_key(item.new_category_name, CATEGORY_TYPE_FOR_DIRECTION[rows_by_id[transaction_id]["direction"]])
        if name_key not in by_name:
            to_create.setdefault(name_key, item.new_category_name.strip())
    created = (
        await create_categories(
            conn, user_id=user_id, categories=[(name, t) for (_, t), name in to_create.items()]
        )
        if to_create
        else []
    )
    for c in created:
        by_id[c["id"]] = c
        by_name[_name_key(c["name"], c["type"])] = c

    category_by_transaction: dict[UUID, UUID | None] = {}
    for transaction_id, item in item_by_id.items():
        if item.category_id is not None:
            category_by_transaction[transaction_id] = item.category_id
        else:
            direction = rows_by_id[transaction_id]["direction"]
            category_by_transaction[transaction_id] = by_name[
                _name_key(item.new_category_name, CATEGORY_TYPE_FOR_DIRECTION[direction])
            ]["id"]

    plan = plan_bulk_allocation(category_by_transaction, transactions_by_id=rows_by_id, categories_by_id=by_id)
    if plan.transaction_ids:
        await insert_bulk_allocations(
            conn,
            transaction_ids=plan.transaction_ids,
            category_ids=plan.category_ids,
            amounts=plan.amounts,
            currencies=plan.currencies,
        )

    # Names: record what this statement printed for each business number,
    # borrow learned names for rows showing only a number (as the preview
    # did), and add numbers 3+ users saw under a listed name to the list.
    sightings = name_sightings(rows, printed, directory)
    await record_name_sightings(conn, user_id=user_id, sightings=sightings)
    sighted = {(s["number_kind"], s["number"], s["direction"]) for s in sightings}
    learned = await get_learned_names(
        conn, numbers=sorted(sighted | set(learned_name_lookups(rows, printed, directory)))
    )
    keys = apply_learned_names(rows, printed, learned, directory)
    await add_users_number_keys(
        conn, keys=plan_key_growth({k: v for k, v in learned.items() if k in sighted}, directory)
    )

    # Only rows actually allocated now teach anything.
    confirmed = []
    for transaction_id in plan.updated:
        item, row = item_by_id[transaction_id], rows_by_id[transaction_id]
        category = by_id[category_by_transaction[transaction_id]]
        confirmed.append(ConfirmedRow(
            key=keys[transaction_id],
            direction=row["direction"],
            amount=float(row["amount"]),
            category_id=category["id"],
            category_name=category["name"],
            label=item.label,
            user_entered_paybill=item.user_entered_paybill,
        ))
    learning = plan_learning(confirmed, directory)
    await upsert_rules(conn, user_id=user_id, rules=learning.rules)
    await set_rule_labels(conn, user_id=user_id, labels=learning.labels)
    await upsert_merchant_votes(conn, user_id=user_id, votes=learning.merchant_votes)
    await upsert_number_votes(conn, user_id=user_id, votes=learning.number_votes)
    reviewed_at = await mark_import_reviewed(conn, import_id=statement_import["id"])
    # ab-265: finished - the saved progress has done its job.
    await delete_progress(conn, import_id=statement_import["id"])

    return ConfirmResult(
        allocated=plan.updated,
        skipped=plan.skipped,
        created_categories=created,
        learned=sum(r["times"] for r in learning.rules),
        reviewed_at=reviewed_at,
        plan=plan,
        transactions_by_id=rows_by_id,
    )
