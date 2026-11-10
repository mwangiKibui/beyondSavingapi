"""What one statement confirm teaches (ab-253). Pure: returns the rows to
write; repositories.category_suggestions writes them.

  rules          the user's own memory, one per (key, account, direction,
                 category), with how many rows and their total amount
  labels         the user's "Who is this?" label / typed Paybill, per key
  merchant_votes one vote per business name
  number_votes   one vote per Paybill/Till number
Votes are read back at preview time: once 3+ distinct users voted and one
category clearly leads, it's suggested to everyone (source "others") and
overrides the shared list - no admin gate (2026-11-10). Numbers the list
doesn't know also feed the admin "Unknown numbers" lead (counts only).

Person-named Tills and reference rows (transfers, NCBA account refs)
never vote: their keys stay private to the user.
"""

from collections import Counter
from dataclasses import dataclass, field
from uuid import UUID

from app.services.category_suggestions import Directory, rule_key, vote_keys
from app.services.merchant_keys import RowKey


@dataclass(frozen=True)
class ConfirmedRow:
    key: RowKey
    direction: str
    amount: float
    category_id: UUID
    category_name: str
    label: str | None = None
    user_entered_paybill: str | None = None


@dataclass
class Learning:
    rules: list[dict] = field(default_factory=list)
    labels: list[dict] = field(default_factory=list)
    merchant_votes: list[dict] = field(default_factory=list)
    number_votes: list[dict] = field(default_factory=list)


def _one_vote(counter: Counter) -> str:
    # Most common; on a tie, the first seen (Counter keeps insertion order).
    return counter.most_common(1)[0][0]


def plan_learning(rows: list[ConfirmedRow], directory: Directory) -> Learning:
    rules: dict[tuple, dict] = {}
    labels: dict[tuple, dict] = {}
    merchant: dict[tuple, Counter] = {}
    numbers: dict[tuple, Counter] = {}

    for r in rows:
        key = r.key
        rk = rule_key(key, directory)
        if rk is None:
            continue
        kind, value, provider = rk

        rule = rules.setdefault(
            (kind, value, provider, key.account_hash, r.direction, r.category_id),
            {"key_kind": kind, "key_value": value, "provider": provider, "account_hash": key.account_hash,
             "direction": r.direction, "category_id": r.category_id, "times": 0, "total": 0.0},
        )
        rule["times"] += 1
        rule["total"] += float(r.amount)

        if r.label or r.user_entered_paybill:
            entry = labels.setdefault(
                (kind, value, provider, r.direction),
                {"key_kind": kind, "key_value": value, "provider": provider, "direction": r.direction,
                 "label": None, "user_entered_paybill": None},
            )
            entry["label"] = r.label or entry["label"]
            entry["user_entered_paybill"] = r.user_entered_paybill or entry["user_entered_paybill"]

        name_key, number_key = vote_keys(key, r.direction, directory)
        if name_key:
            merchant.setdefault((key.name, r.direction), Counter())[r.category_name] += 1
        if number_key:
            numbers.setdefault((key.number_kind, key.number, r.direction), Counter())[r.category_name] += 1

    return Learning(
        rules=list(rules.values()),
        labels=list(labels.values()),
        merchant_votes=[
            {"merchant_key": name, "direction": direction, "category_name": _one_vote(c)}
            for (name, direction), c in merchant.items()
        ],
        number_votes=[
            {"number_kind": kind, "number": number, "direction": direction, "category_name": _one_vote(c)}
            for (kind, number, direction), c in numbers.items()
        ],
    )
