"""'Use this from now on' (ab-253): when a user categorises a row later -
outside the import confirm - and ticks update_rule, the chosen category
becomes the TOP one remembered for that payee, so the next statement
suggests it even if another category was used more often before.

Only the user's own memory changes; no shared-list votes are cast here.
"""

from uuid import UUID

import asyncpg

from app.repositories.category_suggestions import get_blocked_numbers, get_rows_for_rule_update, set_preferred_rules
from app.services.category_suggestions import Directory, rule_key
from app.services.merchant_keys import extract_key


def plan_preferred(
    rows: list[dict], category_by_transaction: dict[UUID, UUID], *, secret: str, directory: Directory
) -> list[dict]:
    """One preference per (key, account, direction); a key chosen twice in
    one request keeps the last choice. Rows with no key are skipped."""
    plans: dict[tuple, dict] = {}
    for row in rows:
        category_id = category_by_transaction.get(row["id"])
        if category_id is None:
            continue
        key = extract_key(provider=row["provider"], description=row["description"], direction=row["direction"],
                          secret=secret)
        rk = rule_key(key, directory)
        if rk is None:
            continue
        kind, value, provider = rk
        plans[(kind, value, provider, key.account_hash, row["direction"])] = {
            "key_kind": kind, "key_value": value, "provider": provider, "account_hash": key.account_hash,
            "direction": row["direction"], "category_id": category_id, "amount": float(row["amount"]),
        }
    return list(plans.values())


async def remember_preferred(
    pool: asyncpg.Pool, *, user_id: UUID, category_by_transaction: dict[UUID, UUID], secret: str
) -> int:
    """Returns how many payees' preferences were set."""
    if not category_by_transaction:
        return 0
    rows = await get_rows_for_rule_update(pool, transaction_ids=list(category_by_transaction), user_id=user_id)
    directory = Directory.from_rows([], blocked=await get_blocked_numbers(pool))
    plans = plan_preferred(rows, category_by_transaction, secret=secret, directory=directory)
    await set_preferred_rules(pool, user_id=user_id, preferences=plans)
    return len(plans)
