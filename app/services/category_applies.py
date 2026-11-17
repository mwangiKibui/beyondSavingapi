"""Applying a category to payee groups (ab-282), saved straight away and
undoable, from an import's review or from Transactions -> Uncategorised.

Each group in a request becomes one category_applies row:
  remember off  "just this time" - rows are allocated, nothing is learned
  remember on   the category becomes the payee's top saved choice (ab-270)
                and business payees cast a shared vote. Person rows and
                account references never vote (vote_keys), so nothing about
                a person is shared or matched across users.
Undo deletes exactly the apply's allocations and restores the saved
choices and votes snapshotted before it. Runs on ONE connection the
caller holds a transaction on.
"""

from collections import Counter
from dataclasses import dataclass, field, replace
from uuid import UUID

import asyncpg

from app.repositories.category_suggestions import (
    create_categories,
    get_approved_directory,
    get_blocked_numbers,
    get_import_rows,
    get_learned_names,
    get_shared_votes,
    get_user_categories,
    get_user_rules,
    learn_rules,
    set_preferred_rules,
    upsert_merchant_votes,
    upsert_number_votes,
)
from app.repositories.payee_groups import (
    count_apply_allocations,
    delete_apply_allocations,
    delete_apply_line_allocations,
    get_merchant_votes,
    get_number_votes,
    get_rule_rows,
    get_uncategorised_rows,
    insert_applied_allocations,
    insert_apply,
    lock_apply,
    lock_rows_for_apply,
    mark_apply_undone,
    restore_snapshot,
)
from app.services.allocations import CATEGORY_TYPE_FOR_DIRECTION, AllocationPlan, AllocationRejected
from app.services.category_suggestions import (
    Directory,
    _is_open,
    apply_learned_names,
    build_preview_rows,
    learned_name_lookups,
    rule_key,
    rule_lookup_pairs,
    shared_vote_lookups,
    vote_keys,
)
from app.services.merchant_keys import RowKey
from app.services.payee_groups import build_groups, extract_row_keys, group_suggestion, payee_group_key
from app.services.suggestion_learning import ConfirmedRow, plan_learning


@dataclass(frozen=True)
class ApplyGroup:
    transaction_ids: list[UUID]
    category_id: UUID | None = None
    new_category_name: str | None = None
    use_suggestion: bool = False
    remember: bool = False
    # ab-292: a choice for one line of a payee - remember learns softly
    # (learn_rules) and casts no shared vote.
    per_line: bool = False
    # Internal: the suggestion use_suggestion resolved to for these rows.
    suggestion: dict | None = None


@dataclass
class ApplyResult:
    groups: list[dict]
    created_categories: list[dict]
    plan: AllocationPlan = field(repr=False)
    transactions_by_id: dict = field(repr=False)


async def load_suggestions(
    conn: asyncpg.Connection | asyncpg.Pool, *, user_id: UUID, rows: list[dict], keys: dict[UUID, RowKey]
) -> tuple[dict[UUID, RowKey], dict[UUID, dict], list[dict], Directory]:
    """The review's suggestion pipeline (ab-253) for any set of the user's
    rows: learned names, the user's rules, others' picks, the shared list.
    Returns (keys with learned names, preview row by id, categories,
    directory)."""
    directory = Directory.from_rows(await get_approved_directory(conn), blocked=await get_blocked_numbers(conn))
    learned = await get_learned_names(conn, numbers=learned_name_lookups(rows, keys, directory))
    keys = apply_learned_names(rows, keys, learned, directory)
    rules = await get_user_rules(conn, user_id=user_id, pairs=rule_lookup_pairs(keys.values(), directory))
    categories = await get_user_categories(conn, user_id=user_id)
    name_keys, number_keys = shared_vote_lookups(rows, keys, directory)
    votes = await get_shared_votes(conn, name_keys=name_keys, number_keys=number_keys)
    preview = build_preview_rows(
        rows, keys=keys, rules=rules, directory=directory, categories=categories, shared_votes=votes
    )
    return keys, {p["transaction_id"]: p for p in preview}, categories, directory


async def uncategorised_groups(
    conn: asyncpg.Connection | asyncpg.Pool, *, user_id: UUID, secret: str
) -> tuple[list[dict], int]:
    """Every payee group still waiting in Transactions -> Uncategorised,
    and how many rows that is (the Transactions badge)."""
    rows = await get_uncategorised_rows(conn, user_id=user_id)
    if not rows:
        return [], 0
    keys, preview, _, _ = await load_suggestions(
        conn, user_id=user_id, rows=rows, keys=extract_row_keys(rows, secret=secret)
    )
    return build_groups(rows, keys=keys, preview=preview), len(rows)


def _name_key(name: str, category_type: str) -> tuple[str, str]:
    return (name.strip().lower(), category_type)


async def apply_groups(
    conn: asyncpg.Connection,
    *,
    user_id: UUID,
    groups: list[ApplyGroup],
    secret: str,
    statement_import: dict | None = None,
) -> ApplyResult:
    """statement_import: set when applied from that import's review (the
    caller has checked it's the user's); its rows are the only ones allowed."""
    all_ids = [tid for g in groups for tid in g.transaction_ids]
    if len(set(all_ids)) != len(all_ids):
        raise AllocationRejected(422, "A row appears more than once")

    rows = await lock_rows_for_apply(conn, user_id=user_id, transaction_ids=all_ids)
    by_id = {r["id"]: r for r in rows}
    if set(all_ids) - by_id.keys():
        raise AllocationRejected(404, "Transaction not found")
    if statement_import is not None:
        in_scope = {
            r["id"]
            for r in await get_import_rows(
                conn,
                import_id=statement_import["id"],
                account_id=statement_import["account_id"],
                period_start=statement_import["period_start"],
                period_end=statement_import["period_end"],
            )
        }
        if set(all_ids) - in_scope:
            raise AllocationRejected(422, "One or more rows aren't part of this statement")
    if not all(_is_open(r) for r in rows):
        raise AllocationRejected(409, "One or more rows are already categorised")

    raw_keys = extract_row_keys(rows, secret=secret)
    group_keys = []
    for g in groups:
        found = {payee_group_key(raw_keys[t], by_id[t]["direction"], t) for t in g.transaction_ids}
        if len(found) != 1:
            raise AllocationRejected(422, "Each group must be rows of one payee")
        group_keys.append(found.pop())

    keys, preview, categories, directory = await load_suggestions(conn, user_id=user_id, rows=rows, keys=raw_keys)
    groups, group_keys = _split_by_suggestion(groups, group_keys, preview)
    by_cat_id = {c["id"]: c for c in categories}
    by_name = {_name_key(c["name"], c["type"]): c for c in categories}

    # Each group's target: an existing category id, or a name to get-or-create.
    targets: list[tuple[str, object]] = []
    to_create: dict[tuple[str, str], str] = {}
    for g in groups:
        wanted = CATEGORY_TYPE_FOR_DIRECTION[by_id[g.transaction_ids[0]]["direction"]]
        name = None
        if g.use_suggestion:
            s = g.suggestion
            if s["category_id"] is not None:
                targets.append(("id", s["category_id"]))
                continue
            name = s["category_name"]
        elif g.new_category_name is not None:
            name = g.new_category_name
        if name is None:
            targets.append(("id", g.category_id))
            continue
        nk = _name_key(name, wanted)
        targets.append(("name", nk))
        if nk not in by_name:
            to_create.setdefault(nk, name.strip())

    created = (
        await create_categories(conn, user_id=user_id, categories=[(n, t) for (_, t), n in to_create.items()])
        if to_create
        else []
    )
    for c in created:
        by_cat_id[c["id"]] = c
        by_name[_name_key(c["name"], c["type"])] = c

    category_ids = [by_name[value]["id"] if kind == "name" else value for kind, value in targets]
    for g, category_id in zip(groups, category_ids):
        category = by_cat_id.get(category_id)
        if category is None:
            raise AllocationRejected(404, "Category not found")
        if category["type"] != CATEGORY_TYPE_FOR_DIRECTION[by_id[g.transaction_ids[0]]["direction"]]:
            raise AllocationRejected(
                422, "A category's type doesn't match one of the selected transactions' direction"
            )

    plan = AllocationPlan()
    results = []
    remembered = []
    for g, gk, category_id in zip(groups, group_keys, category_ids):
        category = by_cat_id[category_id]
        members = [by_id[t] for t in g.transaction_ids]
        allocatable = [r for r in members if float(r["amount"]) - float(r["allocated"]) > 0]
        snapshot = await _snapshot(conn, user_id=user_id, rows=members, keys=keys, directory=directory) \
            if g.remember else {}
        apply_id = await insert_apply(
            conn, user_id=user_id, import_id=statement_import["id"] if statement_import else None,
            category_id=category_id, group_key=gk, remember=g.remember, snapshot=snapshot,
        )
        if allocatable:
            amounts = [float(r["amount"]) - float(r["allocated"]) for r in allocatable]
            await insert_applied_allocations(
                conn, apply_id=apply_id, transaction_ids=[r["id"] for r in allocatable], category_id=category_id,
                amounts=amounts, currencies=[r["currency"] for r in allocatable],
            )
            for r, amount in zip(allocatable, amounts):
                plan.transaction_ids.append(r["id"])
                plan.category_ids.append(category_id)
                plan.amounts.append(amount)
                plan.currencies.append(r["currency"])
                plan.updated.append(r["id"])
        skipped = [r["id"] for r in members if r not in allocatable]
        plan.skipped.extend(skipped)
        if g.remember and allocatable:
            await _remember(conn, user_id=user_id, rows=allocatable, keys=keys, directory=directory,
                            category=category, per_line=g.per_line)
            remembered.append(len(results))
        results.append({
            "apply_id": apply_id,
            "group_key": gk,
            "category_id": category_id,
            "category_name": category["name"],
            "remember": g.remember,
            "allocated": [r["id"] for r in allocatable],
            "skipped": skipped,
            "earlier_waiting": 0,
        })

    if remembered:
        waiting = await _waiting_counts(conn, user_id=user_id, secret=secret, exclude=set(all_ids))
        for i in remembered:
            results[i]["earlier_waiting"] = waiting.get(results[i]["group_key"], 0)

    return ApplyResult(groups=results, created_categories=created, plan=plan, transactions_by_id=by_id)


def _split_by_suggestion(groups: list[ApplyGroup], group_keys: list[str], preview: dict):
    """use_suggestion files each line under its own suggestion (ab-292):
    a group whose lines suggest different categories becomes one apply per
    category. Lines with no suggestion of their own take the group's."""
    out_groups, out_keys = [], []
    for g, gk in zip(groups, group_keys):
        if not g.use_suggestion:
            out_groups.append(g)
            out_keys.append(gk)
            continue
        own = {t: preview.get(t, {}).get("suggestion") for t in g.transaction_ids}
        fallback = group_suggestion(list(own.values()))
        if fallback is None and not any(own.values()):
            raise AllocationRejected(422, "One of the groups has no suggestion")
        split: dict[tuple, tuple[dict, list[UUID]]] = {}
        for t in g.transaction_ids:
            s = own[t] or fallback
            if s is None:
                raise AllocationRejected(422, "One of the groups has no suggestion")
            sid = (s["category_id"], s["category_name"].strip().lower())
            split.setdefault(sid, (s, []))[1].append(t)
        for s, ids in split.values():
            out_groups.append(replace(g, transaction_ids=ids, suggestion=s))
            out_keys.append(gk)
    return out_groups, out_keys


async def _snapshot(conn, *, user_id: UUID, rows: list[dict], keys: dict, directory: Directory) -> dict:
    """The user's saved choices and votes the apply is about to change."""
    rule_keys, merchant_keys, number_keys = {}, {}, {}
    for r in rows:
        key = keys[r["id"]]
        rk = rule_key(key, directory)
        if rk:
            rule_keys[(*rk, key.account_hash, r["direction"])] = {
                "key_kind": rk[0], "key_value": rk[1], "provider": rk[2], "account_hash": key.account_hash,
                "direction": r["direction"],
            }
        name_key, number_key = vote_keys(key, r["direction"], directory)
        if name_key:
            merchant_keys[(key.name, r["direction"])] = {"merchant_key": key.name, "direction": r["direction"]}
        if number_key:
            number_keys[number_key] = {"number_kind": number_key[0], "number": number_key[1],
                                       "direction": number_key[2]}
    rule_keys, merchant_keys, number_keys = (
        list(rule_keys.values()), list(merchant_keys.values()), list(number_keys.values())
    )
    rules = await get_rule_rows(conn, user_id=user_id, rule_keys=rule_keys)
    m_votes = {(v["merchant_key"], v["direction"]): v["category_name"]
               for v in await get_merchant_votes(conn, user_id=user_id, keys=merchant_keys)}
    n_votes = {(v["number_kind"], v["number"], v["direction"]): v["category_name"]
               for v in await get_number_votes(conn, user_id=user_id, keys=number_keys)}
    return {
        "rule_keys": rule_keys,
        "rules": rules,
        "merchant_votes": [dict(k, category_name=m_votes.get((k["merchant_key"], k["direction"])))
                           for k in merchant_keys],
        "number_votes": [dict(k, category_name=n_votes.get((k["number_kind"], k["number"], k["direction"])))
                         for k in number_keys],
    }


async def _remember(conn, *, user_id: UUID, rows: list[dict], keys: dict, directory: Directory,
                    category: dict, per_line: bool = False) -> None:
    """Top saved choice per payee key (one per account reference), plus
    the shared votes a confirm would cast for business payees."""
    preferences: dict[tuple, dict] = {}
    amounts: dict[tuple, list[float]] = {}
    for r in rows:
        key = keys[r["id"]]
        rk = rule_key(key, directory)
        if rk is None:
            continue
        k = (*rk, key.account_hash, r["direction"])
        preferences[k] = {
            "key_kind": rk[0], "key_value": rk[1], "provider": rk[2], "account_hash": key.account_hash,
            "direction": r["direction"], "category_id": category["id"],
        }
        amounts.setdefault(k, []).append(float(r["amount"]))
    for k, p in preferences.items():
        p["amount"] = sum(amounts[k]) / len(amounts[k])
    if per_line:
        await learn_rules(conn, user_id=user_id, preferences=list(preferences.values()))
        return
    await set_preferred_rules(conn, user_id=user_id, preferences=list(preferences.values()))

    learning = plan_learning(
        [
            ConfirmedRow(key=keys[r["id"]], direction=r["direction"], amount=float(r["amount"]),
                         category_id=category["id"], category_name=category["name"])
            for r in rows
        ],
        directory,
    )
    await upsert_merchant_votes(conn, user_id=user_id, votes=learning.merchant_votes)
    await upsert_number_votes(conn, user_id=user_id, votes=learning.number_votes)


async def _waiting_counts(conn, *, user_id: UUID, secret: str, exclude: set[UUID]) -> Counter:
    """Rows per payee group still waiting in Uncategorised."""
    rows = [r for r in await get_uncategorised_rows(conn, user_id=user_id) if r["id"] not in exclude]
    keys = extract_row_keys(rows, secret=secret)
    return Counter(payee_group_key(keys[r["id"]], r["direction"], r["id"]) for r in rows)


async def undo_apply(conn: asyncpg.Connection, *, user_id: UUID, apply_id: UUID) -> list[UUID]:
    """Returns the rows that are open again."""
    applied = await lock_apply(conn, apply_id=apply_id, user_id=user_id)
    if applied is None:
        raise AllocationRejected(404, "Not found")
    if applied["undone_at"] is not None:
        raise AllocationRejected(409, "This was already undone")
    reopened = await delete_apply_allocations(conn, apply_id=apply_id)
    if applied["remember"]:
        await restore_snapshot(conn, user_id=user_id, snapshot=applied["snapshot"])
    await mark_apply_undone(conn, apply_id=apply_id)
    return reopened


async def reopen_lines(
    conn: asyncpg.Connection, *, user_id: UUID, apply_id: UUID, transaction_ids: list[UUID]
) -> list[UUID]:
    """ab-292: puts some lines of an apply back to open, e.g. to file one
    line differently. The rest stay filed and the saved choices stand;
    reopening the last lines is the same as undoing the apply."""
    applied = await lock_apply(conn, apply_id=apply_id, user_id=user_id)
    if applied is None:
        raise AllocationRejected(404, "Not found")
    if applied["undone_at"] is not None:
        raise AllocationRejected(409, "This was already undone")
    reopened = await delete_apply_line_allocations(conn, apply_id=apply_id, transaction_ids=transaction_ids)
    if set(transaction_ids) - set(reopened):
        raise AllocationRejected(422, "One or more rows aren't part of this choice")
    if await count_apply_allocations(conn, apply_id=apply_id) == 0:
        if applied["remember"]:
            await restore_snapshot(conn, user_id=user_id, snapshot=applied["snapshot"])
        await mark_apply_undone(conn, apply_id=apply_id)
    return reopened


async def apply_earlier(conn: asyncpg.Connection, *, user_id: UUID, apply_id: UUID, secret: str) -> ApplyResult:
    """Files the payee's rows still waiting in Uncategorised under the same
    category, as a new apply (its own undo). Nothing more is learned - the
    original apply already remembered the payee."""
    applied = await lock_apply(conn, apply_id=apply_id, user_id=user_id)
    if applied is None:
        raise AllocationRejected(404, "Not found")
    if applied["undone_at"] is not None:
        raise AllocationRejected(409, "This was undone")
    rows = await get_uncategorised_rows(conn, user_id=user_id)
    keys = extract_row_keys(rows, secret=secret)
    waiting = [r["id"] for r in rows if payee_group_key(keys[r["id"]], r["direction"], r["id"]) == applied["group_key"]]
    if not waiting:
        return ApplyResult(groups=[], created_categories=[], plan=AllocationPlan(), transactions_by_id={})
    return await apply_groups(
        conn, user_id=user_id, secret=secret,
        groups=[ApplyGroup(transaction_ids=waiting, category_id=applied["category_id"])],
    )
