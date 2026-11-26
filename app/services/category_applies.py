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
    get_import_rows,
    get_learned_names,
    get_owner,
    get_shared_votes,
    get_user_categories,
    get_user_rules,
    learn_rules,
    set_preferred_rules,
    upsert_merchant_votes,
    upsert_number_votes,
)
from app.repositories.accounts import DuplicateAccount, create_account, get_account
from app.services.ai_categories import load_ai_context
from app.services.limits import ACCOUNTS_MAX, enforce_limit
from app.repositories.row_handling import delete_allocations, get_or_create_transfer_reason
from app.repositories.transfer_reasons import get_user_transfer_reasons
from app.repositories.description_keywords import (
    delete_keyword_choices,
    get_keyword_choice_rows,
    save_keyword_choices,
)
from app.repositories.payee_groups import (
    count_apply_allocations,
    delete_apply_allocations,
    delete_apply_line_allocations,
    get_apply_source,
    get_categorised_here_rows,
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
    set_rows_handled,
)
from app.repositories.transfer_reasons import BETWEEN_MY_ACCOUNTS
from app.services.sub_ledger_roles import SAVING_ROLES
from app.services.allocations import CATEGORY_TYPE_FOR_DIRECTION, AllocationPlan, AllocationRejected
from app.services.import_handling import pair_hand_filed_transfers, unpair_hand_filed
from app.services.description_keywords import load_keyword_context
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
from app.services.directory_cache import get_directory
from app.services.merchant_keys import RowKey
from app.services.payee_groups import (
    _suggestion_id,
    build_groups,
    extract_row_keys,
    group_suggestion,
    payee_group_key,
)
from app.services.suggestion_learning import ConfirmedRow, plan_learning


@dataclass(frozen=True)
class ApplyGroup:
    transaction_ids: list[UUID]
    category_id: UUID | None = None
    new_category_name: str | None = None
    use_suggestion: bool = False
    remember: bool = False
    # ab-292: a choice for one line of a payee - casts no shared vote and
    # teaches no keyword. ab-298: remembered, it still becomes the payee's
    # suggestion (the latest choice wins).
    per_line: bool = False
    # Set only when "use suggestion" splits a payee's lines across
    # categories by amount: the minority ones count one more use
    # (learn_rules) instead of becoming the default.
    learn_softly: bool = False
    # ab-285: file the rows as a Funds transfer under this reason instead.
    transfer_reason_id: UUID | None = None
    # ab-285, transfers only - the other side, at most one of: one of the
    # user's accounts, an account added now (nickname, account_type,
    # provider, account_number, currency), or someone else (private name).
    other_account_id: UUID | None = None
    new_account: dict | None = None
    someone_else_name: str | None = None
    # ab-286: money in filed as a refund of an EXPENSE category - it lowers
    # that category's spending. Never remembered.
    refund: bool = False
    # ab-288: what the money is - expense, income, refund, transfer, saving,
    # borrowed, loan_repayment. None: worked out from the fields above.
    kind: str | None = None
    # ab-288: the user's own words for the rows (mainly for "Other"), saved
    # on each allocation. Private: never learned, voted or sent anywhere.
    note: str | None = None
    # ab-335: the rows were already allocated some other way (an earlier
    # statement, the Transactions page): their allocations are replaced.
    replace: bool = False
    # Internal: the suggestion use_suggestion resolved to for these rows.
    suggestion: dict | None = None


@dataclass
class ApplyResult:
    groups: list[dict]
    created_categories: list[dict]
    plan: AllocationPlan = field(repr=False)
    transactions_by_id: dict = field(repr=False)
    created_accounts: list[dict] = field(default_factory=list)


@dataclass(frozen=True)
class _Target:
    """What one group files under: a category or a transfer reason."""
    kind: str  # "category" | "transfer"
    id: UUID


KINDS = ("expense", "income", "refund", "transfer", "saving", "borrowed", "loan_repayment")
# Saving / Borrowed file the row by how it's handled, with no category.
HANDLED_AS_FOR_KIND = {"saving": "saving", "borrowed": "loan"}


def _normalise(g: ApplyGroup, row: dict) -> ApplyGroup:
    """Checks a group's kind (ab-288) against its rows and folds it into the
    fields the rest of apply_groups reads."""
    kind, direction = g.kind, row["direction"]
    if kind is None:
        return g
    if kind not in KINDS:
        raise AllocationRejected(422, "Unknown kind")
    allowed = {
        "out": {"expense", "transfer", "saving", "loan_repayment"},
        "in": {"income", "transfer", "refund", "borrowed", "saving"},
    }[direction]
    if kind not in allowed:
        raise AllocationRejected(422, "That type isn't for money going this way")
    if kind == "saving" and direction == "in" and row.get("sub_ledger_role") not in SAVING_ROLES:
        raise AllocationRejected(422, "Money in is saving only on a SACCO deposits or shares sub-ledger")
    if kind in HANDLED_AS_FOR_KIND:
        if g.remember:
            raise AllocationRejected(422, "Saving and Borrowed aren't remembered")
        if g.category_id or g.new_category_name or g.use_suggestion or g.transfer_reason_id:
            raise AllocationRejected(422, "Saving and Borrowed take no category or reason")
        return g
    if kind == "refund":
        return replace(g, refund=True)
    if kind == "transfer" and g.transfer_reason_id is None and not g.use_suggestion:
        return g  # filed under "Between my accounts" (see apply_groups)
    return g


def _is_transfer(g: ApplyGroup) -> bool:
    if g.kind == "transfer":
        return True
    return g.transfer_reason_id is not None or bool(
        g.use_suggestion and g.suggestion and g.suggestion.get("kind") == "transfer"
    )


async def load_suggestions(
    conn: asyncpg.Connection | asyncpg.Pool, *, user_id: UUID, rows: list[dict], keys: dict[UUID, RowKey]
) -> tuple[dict[UUID, RowKey], dict[UUID, dict], list[dict], Directory]:
    """The review's suggestion pipeline (ab-253) for any set of the user's
    rows: learned names, the user's rules, others' picks, the shared list.
    Returns (keys with learned names, preview row by id, categories,
    directory)."""
    directory = await get_directory(conn)
    learned = await get_learned_names(conn, numbers=learned_name_lookups(rows, keys, directory))
    keys = apply_learned_names(rows, keys, learned, directory)
    rules = await get_user_rules(conn, user_id=user_id, pairs=rule_lookup_pairs(keys.values(), directory))
    categories = await get_user_categories(conn, user_id=user_id)
    name_keys, number_keys = shared_vote_lookups(rows, keys, directory)
    votes = await get_shared_votes(conn, name_keys=name_keys, number_keys=number_keys)
    keywords = await load_keyword_context(conn, user_id=user_id, rows=rows)
    reasons = await get_user_transfer_reasons(conn, user_id=user_id)
    owner = await get_owner(conn, user_id=user_id)
    ai = await load_ai_context(conn, user_id=user_id, rows=rows, keys=keys, categories=categories)
    preview = build_preview_rows(
        rows, keys=keys, rules=rules, directory=directory, categories=categories, shared_votes=votes,
        keywords=keywords, reasons=reasons, owner=owner, ai=ai,
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


async def categorised_here_groups(
    conn: asyncpg.Connection | asyncpg.Pool, *, user_id: UUID, secret: str
) -> list[dict]:
    """ab-284: Uncategorised -> "Categorised here": one group per apply the
    user made from the tab that is still filed, newest first, each
    carrying its apply_id (undo) and when it was filed (applied_at)."""
    rows = await get_categorised_here_rows(conn, user_id=user_id)
    if not rows:
        return []
    # Only the names (learned ones included); filed rows need no suggestion.
    directory = await get_directory(conn)
    keys = extract_row_keys(rows, secret=secret)
    learned = await get_learned_names(conn, numbers=learned_name_lookups(rows, keys, directory))
    keys = apply_learned_names(rows, keys, learned, directory)
    categories = await get_user_categories(conn, user_id=user_id)
    by_apply: dict[UUID, list[dict]] = {}
    for r in rows:
        by_apply.setdefault(r["apply_id"], []).append(r)
    names = {c["id"]: c["name"] for c in categories}
    types = {c["id"]: c["type"] for c in categories}
    entry_types = {c["id"]: c.get("entry_type") for c in categories}
    groups = []
    for apply_id, members in by_apply.items():
        for g in build_groups(members, keys=keys, category_names=names, category_types=types,
                              category_entry_types=entry_types):
            # The same payee can be filed twice: each apply is its own group.
            groups.append({**g, "group_key": f"a:{apply_id}:{g['group_key']}",
                           "applied_at": members[0]["applied_at"]})
    return groups


def _apply_kind(g: ApplyGroup, target: "_Target", category: dict | None, direction: str) -> str:
    if target.kind == "handling":
        return g.kind
    if target.kind == "transfer":
        return "transfer"
    if g.refund:
        return "refund"
    if direction == "out" and category.get("entry_type") == "loan_repayment":
        return "loan_repayment"
    return category["type"]


def _wanted_type(direction: str, refund: bool) -> str:
    """The category type a group needs: a refund (ab-286) takes an
    expense category on money in."""
    return "expense" if refund else CATEGORY_TYPE_FOR_DIRECTION[direction]


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
    # ab-335: editing rows already allocated - their allocations go first.
    # Rows handled at import or paired as a transfer leg keep their own
    # ways back ("Not a charge…", Undo).
    replacing = [tid for g in groups if g.replace for tid in g.transaction_ids]
    if replacing:
        if any(by_id[t]["handled_as"] or by_id[t]["is_transfer"] for t in replacing):
            raise AllocationRejected(409, "One or more rows can't be changed here")
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
    if replacing:
        await delete_allocations(conn, transaction_ids=replacing)
        rows = await lock_rows_for_apply(conn, user_id=user_id, transaction_ids=all_ids)
        by_id = {r["id"]: r for r in rows}
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
    groups = [_normalise(g, by_id[g.transaction_ids[0]]) for g in groups]
    groups, group_keys = _split_by_suggestion(groups, group_keys, preview)
    by_cat_id = {c["id"]: c for c in categories}
    by_name = {_name_key(c["name"], c["type"]): c for c in categories}

    # Each group's target: an existing category id, or a name to get-or-create,
    # or a transfer reason (ab-285) by id or by name.
    targets: list[tuple[str, object]] = []
    to_create: dict[tuple[str, str], str] = {}
    for g in groups:
        has_other_side = g.other_account_id is not None or g.new_account is not None or g.someone_else_name
        if has_other_side and not _is_transfer(g):
            raise AllocationRejected(422, "Only a Funds transfer takes where the money came from or went to")
        if g.kind in HANDLED_AS_FOR_KIND:
            targets.append(("handling", g.kind))
            continue
        if g.kind == "transfer" and g.transfer_reason_id is None and not g.use_suggestion:
            targets.append(("reason_name", BETWEEN_MY_ACCOUNTS))
            continue
        if g.transfer_reason_id is not None:
            targets.append(("reason", g.transfer_reason_id))
            continue
        if g.use_suggestion and g.suggestion.get("kind") == "transfer":
            s = g.suggestion
            targets.append(("reason", s["transfer_reason_id"]) if s["transfer_reason_id"] is not None
                           else ("reason_name", s["category_name"]))
            continue
        if g.refund and by_id[g.transaction_ids[0]]["direction"] != "in":
            raise AllocationRejected(422, "Only money in can be a refund")
        wanted = _wanted_type(by_id[g.transaction_ids[0]]["direction"], g.refund)
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

    reasons = await _resolve_reasons(conn, user_id=user_id, targets=targets)
    resolved: list[_Target] = []
    for g, (kind, value) in zip(groups, targets):
        direction = by_id[g.transaction_ids[0]]["direction"]
        if kind == "handling":
            resolved.append(_Target("handling", None))
            continue
        if kind in ("reason", "reason_name"):
            reason = reasons.get(value)
            if reason is None:
                raise AllocationRejected(404, "Transfer reason not found")
            if reason["direction"] not in (None, direction):
                raise AllocationRejected(422, "That transfer reason is for money going the other way")
            resolved.append(_Target("transfer", reason["id"]))
            continue
        category = by_cat_id.get(by_name[value]["id"] if kind == "name" else value)
        if category is None:
            raise AllocationRejected(404, "Category not found")
        if category["type"] != _wanted_type(direction, g.refund):
            raise AllocationRejected(
                422, "A category's type doesn't match one of the selected transactions' direction"
            )
        if g.kind == "loan_repayment" and category.get("entry_type") != "loan_repayment":
            raise AllocationRejected(422, "A loan repayment needs a loan-repayment category")
        resolved.append(_Target("category", category["id"]))
    reasons_by_id = {r["id"]: r for r in reasons.values()}

    others, created_accounts = await _resolve_other_sides(conn, user_id=user_id, groups=groups, rows_by_id=by_id)

    plan = AllocationPlan()
    results = []
    remembered = []
    for g, gk, target, other in zip(groups, group_keys, resolved, others):
        is_transfer = target.kind == "transfer"
        is_handling = target.kind == "handling"
        category = by_cat_id[target.id] if target.kind == "category" else None
        reason = reasons_by_id[target.id] if is_transfer else None
        category_id = target.id if target.kind == "category" else None
        reason_id = target.id if is_transfer else None
        kind = _apply_kind(g, target, category, by_id[g.transaction_ids[0]]["direction"])
        members = [by_id[t] for t in g.transaction_ids]
        allocatable = [r for r in members if float(r["amount"]) - float(r["allocated"]) > 0]
        snapshot = await _snapshot(conn, user_id=user_id, rows=members, keys=keys, directory=directory,
                                   preview=preview) if g.remember else {}
        apply_id = await insert_apply(
            conn, user_id=user_id, import_id=statement_import["id"] if statement_import else None,
            category_id=category_id, group_key=gk, remember=g.remember, snapshot=snapshot,
            transfer_reason_id=reason_id, kind=kind,
        )
        if is_handling:
            await set_rows_handled(conn, transaction_ids=[r["id"] for r in members],
                                   handled_as=HANDLED_AS_FOR_KIND[kind])
        if allocatable:
            amounts = [float(r["amount"]) - float(r["allocated"]) for r in allocatable]
            await insert_applied_allocations(
                conn, apply_id=apply_id, transaction_ids=[r["id"] for r in allocatable], category_id=category_id,
                amounts=amounts, currencies=[r["currency"] for r in allocatable], transfer_reason_id=reason_id,
                source_account_id=other["account_id"], source_description=other["description"],
                note=g.note,
            )
            for r, amount in zip(allocatable, amounts):
                # Only spending can cross a budget: no alerts for the rest.
                if category_id is not None and not g.refund:
                    plan.transaction_ids.append(r["id"])
                    plan.category_ids.append(category_id)
                    plan.amounts.append(amount)
                    plan.currencies.append(r["currency"])
                plan.updated.append(r["id"])
        skipped = [r["id"] for r in members if r not in allocatable]
        plan.skipped.extend(skipped)
        if g.remember and allocatable:
            if is_transfer:
                await _remember_transfer(conn, user_id=user_id, rows=allocatable, keys=keys, directory=directory,
                                         reason_id=reason_id, per_line=g.per_line,
                                         learn_softly=g.learn_softly)
            else:
                await _remember(conn, user_id=user_id, rows=allocatable, keys=keys, directory=directory,
                                category=category, per_line=g.per_line, preview=preview,
                                learn_softly=g.learn_softly)
            remembered.append(len(results))
        results.append({
            "apply_id": apply_id,
            "group_key": gk,
            "category_id": category_id,
            "category_name": category["name"] if category else None,
            "transfer_reason_id": reason_id,
            "transfer_reason_name": reason["name"] if reason else None,
            "is_refund": g.refund,
            "kind": kind,
            "other_account_id": other["account_id"],
            "remember": g.remember,
            "allocated": [r["id"] for r in allocatable],
            "skipped": skipped,
            "earlier_waiting": 0,
        })

    # ab-285: a transfer from/to one of the user's accounts pairs with that
    # account's row when its statement is already uploaded.
    if any(o["account_id"] is not None for o in others):
        await pair_hand_filed_transfers(conn, user_id=user_id)

    if remembered:
        waiting = await _waiting_counts(conn, user_id=user_id, secret=secret, exclude=set(all_ids))
        for i in remembered:
            results[i]["earlier_waiting"] = waiting.get(results[i]["group_key"], 0)

    return ApplyResult(groups=results, created_categories=created, plan=plan, transactions_by_id=by_id,
                       created_accounts=created_accounts)


async def _resolve_reasons(conn, *, user_id: UUID, targets: list[tuple[str, object]]) -> dict:
    """The user's transfer reasons the targets name, keyed by the target
    value (id or name). A suggested reason the user doesn't have yet (e.g.
    "Between my accounts") is created, for either direction."""
    if not any(kind in ("reason", "reason_name") for kind, _ in targets):
        return {}
    mine = await get_user_transfer_reasons(conn, user_id=user_id)
    by_id = {r["id"]: r for r in mine}
    by_lower = {r["name"].strip().lower(): r for r in mine}
    out = {}
    for kind, value in targets:
        if kind == "reason" and value in by_id:
            out[value] = by_id[value]
        elif kind == "reason_name":
            reason = by_lower.get(value.strip().lower())
            if reason is None:
                rid = await get_or_create_transfer_reason(conn, user_id=user_id, name=value.strip())
                reason = {"id": rid, "name": value.strip(), "direction": None}
                by_lower[value.strip().lower()] = reason
            out[value] = reason
    return out


async def _resolve_other_sides(conn, *, user_id: UUID, groups: list[ApplyGroup], rows_by_id: dict):
    """Each transfer group's other side as {account_id, description}. The
    account must be the user's (two-user isolation) and not the rows' own
    account; an account added from the drawer is created here, in the same
    save, with the same checks (and accounts_max limit) as POST /accounts."""
    out, created = [], []
    for g in groups:
        given = sum([g.other_account_id is not None, g.new_account is not None, bool(g.someone_else_name)])
        if given > 1:
            raise AllocationRejected(422, "Pick one of: one of your accounts, a new account, or someone else")
        own_accounts = {rows_by_id[t]["account_id"] for t in g.transaction_ids}
        account_id = None
        if g.other_account_id is not None:
            other = await get_account(conn, account_id=g.other_account_id, user_id=user_id)
            if other is None:
                raise AllocationRejected(422, "Account not found")
            account_id = g.other_account_id
            if account_id in own_accounts:
                # Money can't move from an account to itself: name it, and
                # ask for the other side in the money's own direction.
                went = rows_by_id[g.transaction_ids[0]]["direction"] == "out"
                raise AllocationRejected(
                    422,
                    f"These rows are on {other['nickname']}. "
                    f"Choose the account the money {'went to' if went else 'came from'}.",
                )
        elif g.new_account is not None:
            new = dict(g.new_account)
            new["currency"] = new.get("currency") or rows_by_id[g.transaction_ids[0]]["currency"]
            # ab-309: same plan limit as POST /accounts. Raises the 429
            # (an HTTPException), which rolls the whole save back. On conn,
            # so an earlier group's new account in this save counts too.
            await enforce_limit(conn, user_id, ACCOUNTS_MAX)
            try:
                account = await create_account(conn, user_id=user_id, **new)
            except DuplicateAccount:
                raise AllocationRejected(409, "An account with that number already exists") from None
            created.append(account)
            account_id = account["id"]
        out.append({"account_id": account_id, "description": g.someone_else_name or None})
    return out, created


def _split_by_suggestion(groups: list[ApplyGroup], group_keys: list[str], preview: dict):
    """use_suggestion files each line under its own suggestion (ab-292):
    a group whose lines suggest different categories becomes one apply per
    category. Lines with no suggestion of their own take the group's.
    When remembered, only the main (most suggested) category is made the
    payee's default; the others learn softly."""
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
            sid = _suggestion_id(s)
            split.setdefault(sid, (s, []))[1].append(t)
        main = fallback and _suggestion_id(fallback)
        for sid, (s, ids) in split.items():
            # Only the group's main category becomes the payee's default;
            # the others learn as line choices (no forced top, no vote).
            minority = len(split) > 1 and sid != main
            out_groups.append(replace(g, transaction_ids=ids, suggestion=s, per_line=g.per_line or minority,
                                      learn_softly=minority))
            out_keys.append(gk)
    return out_groups, out_keys


def _matched_keywords(rows: list[dict], preview: dict) -> dict[UUID, dict]:
    """Description keywords (ab-290) the rows matched, by keyword id."""
    out = {}
    for r in rows:
        match = (preview.get(r["id"]) or {}).get("keyword_match")
        if match:
            out[match["id"]] = match
    return out


async def _snapshot(conn, *, user_id: UUID, rows: list[dict], keys: dict, directory: Directory,
                    preview: dict | None = None) -> dict:
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
    keyword_ids = sorted(_matched_keywords(rows, preview or {}), key=str)
    return {
        "rule_keys": rule_keys,
        "rules": rules,
        "keyword_ids": keyword_ids,
        "keyword_choices": await get_keyword_choice_rows(conn, user_id=user_id, keyword_ids=keyword_ids),
        "merchant_votes": [dict(k, category_name=m_votes.get((k["merchant_key"], k["direction"])))
                           for k in merchant_keys],
        "number_votes": [dict(k, category_name=n_votes.get((k["number_kind"], k["number"], k["direction"])))
                         for k in number_keys],
    }


async def _remember(conn, *, user_id: UUID, rows: list[dict], keys: dict, directory: Directory,
                    category: dict, per_line: bool = False, preview: dict | None = None,
                    learn_softly: bool = False) -> None:
    """Top saved choice per payee key (one per account reference), plus
    the shared votes a confirm would cast for business payees, plus the
    user's choice for any description keyword the rows matched (ab-290)."""
    if not per_line:
        await _remember_keywords(conn, user_id=user_id, rows=rows, preview=preview or {}, category=category)
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
    # ab-298: the user's latest choice becomes the payee's suggestion - for
    # the whole group or one line. Only the app's own amount split learns
    # softly. The amount rule in pick_rule still separates a merchant used
    # for clearly different amounts.
    save = learn_rules if learn_softly else set_preferred_rules
    await save(conn, user_id=user_id, preferences=list(preferences.values()))
    if per_line:
        return

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


async def _remember_transfer(conn, *, user_id: UUID, rows: list[dict], keys: dict, directory: Directory,
                             reason_id: UUID, per_line: bool = False, learn_softly: bool = False) -> None:
    """ab-285: a remembered transfer is a private saved choice only - no
    shared merchant/number vote and no keyword learning, for any payee."""
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
            "direction": r["direction"], "category_id": None, "transfer_reason_id": reason_id,
        }
        amounts.setdefault(k, []).append(float(r["amount"]))
    for k, p in preferences.items():
        p["amount"] = sum(amounts[k]) / len(amounts[k])
    save = learn_rules if learn_softly else set_preferred_rules
    await save(conn, user_id=user_id, preferences=list(preferences.values()))


async def _remember_keywords(conn, *, user_id: UUID, rows: list[dict], preview: dict, category: dict) -> None:
    """Filed the list's way: the user follows the list again. Filed another
    way: that becomes the user's own choice for the keyword."""
    # Transfer wording (ab-285) is too general to learn a category from.
    matched = {k: m for k, m in _matched_keywords(rows, preview).items() if m.get("target", "category") == "category"}
    chosen = category["name"].strip().lower()
    same = [k for k, m in matched.items() if m["category_name"].strip().lower() == chosen]
    other = [k for k in matched if k not in same]
    await delete_keyword_choices(conn, user_id=user_id, keyword_ids=same)
    await save_keyword_choices(conn, user_id=user_id, keyword_ids=other, category_id=category["id"])


async def _waiting_counts(conn, *, user_id: UUID, secret: str, exclude: set[UUID]) -> Counter:
    """Rows per payee group still waiting in Uncategorised."""
    rows = [r for r in await get_uncategorised_rows(conn, user_id=user_id) if r["id"] not in exclude]
    keys = extract_row_keys(rows, secret=secret)
    return Counter(payee_group_key(keys[r["id"]], r["direction"], r["id"]) for r in rows)


async def undo_apply(
    conn: asyncpg.Connection, *, user_id: UUID, apply_id: UUID, secret: str | None = None
) -> list[UUID]:
    """Returns the rows that are open again (a paired transfer's other
    side included, when it goes back to the review - ab-285)."""
    applied = await lock_apply(conn, apply_id=apply_id, user_id=user_id)
    if applied is None:
        raise AllocationRejected(404, "Not found")
    if applied["undone_at"] is not None:
        raise AllocationRejected(409, "This was already undone")
    reopened = await delete_apply_allocations(conn, apply_id=apply_id)
    if applied.get("kind") in HANDLED_AS_FOR_KIND:
        await set_rows_handled(conn, transaction_ids=reopened, handled_as=None)
    if applied["remember"]:
        await restore_snapshot(conn, user_id=user_id, snapshot=applied["snapshot"])
    await mark_apply_undone(conn, apply_id=apply_id)
    if applied.get("transfer_reason_id") is not None and secret is not None:
        reopened += await unpair_hand_filed(conn, user_id=user_id, transaction_ids=reopened, secret=secret)
    return reopened


async def reopen_lines(
    conn: asyncpg.Connection, *, user_id: UUID, apply_id: UUID, transaction_ids: list[UUID],
    secret: str | None = None,
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
    if applied.get("kind") in HANDLED_AS_FOR_KIND:
        await set_rows_handled(conn, transaction_ids=reopened, handled_as=None)
    if await count_apply_allocations(conn, apply_id=apply_id) == 0:
        if applied["remember"]:
            await restore_snapshot(conn, user_id=user_id, snapshot=applied["snapshot"])
        await mark_apply_undone(conn, apply_id=apply_id)
    if applied.get("transfer_reason_id") is not None and secret is not None:
        reopened += await unpair_hand_filed(conn, user_id=user_id, transaction_ids=reopened, secret=secret)
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
    if applied.get("kind") in HANDLED_AS_FOR_KIND:
        group = ApplyGroup(transaction_ids=waiting, kind=applied["kind"])
    elif applied.get("transfer_reason_id") is not None:
        source = await get_apply_source(conn, apply_id=apply_id)
        group = ApplyGroup(
            transaction_ids=waiting, transfer_reason_id=applied["transfer_reason_id"],
            other_account_id=source["source_account_id"], someone_else_name=source["source_description"],
        )
    else:
        group = ApplyGroup(
            transaction_ids=waiting, category_id=applied["category_id"],
            # ab-286: an expense category on money in was a refund.
            refund=applied.get("category_type") == "expense"
            and next(r["direction"] for r in rows if r["id"] == waiting[0]) == "in",
        )
    return await apply_groups(conn, user_id=user_id, secret=secret, groups=[group])
