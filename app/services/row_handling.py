"""Which statement rows are handled at import without asking (ab-281).

Pure functions, no I/O. A handled row leaves the review with a short
reason the user can read and undo:

  charge    bank/mobile charges and excise duty -> "Bank & mobile charges"
  loan      loan and Fuliza rows. Repayments get "Loan Repayment"; payouts,
            requests, overdraft draws/repayments and SACCO loan-ledger rows
            get no category - borrowed money is neither income nor spending
            (the purchase Fuliza paid for is already counted).
  transfer  money between two of the user's own accounts. target_provider
            is the other side, used to pair the two legs across statements.

Person rows are never handled here: a payment to a person is the user's
call (ab-269). They can only become the other leg of a transfer, which
the pairing step decides from amounts and dates on the user's own
accounts.
"""

import re
from dataclasses import dataclass

from app.services.merchant_keys import MENTOR, MPESA, NCBA, EQUITY, RowKey, normalize_text

CHARGES_CATEGORY = "Bank & mobile charges"
LOAN_REPAYMENT_CATEGORY = "Loan Repayment"
BETWEEN_MY_ACCOUNTS = "Between my accounts"

_LOAN_REASONS = {
    "loan_payout": "Borrowed money, so not income",
    "loan_request": "Loan request - no money moved",
    "overdraft_draw": "Fuliza overdraft - the purchase it paid for is counted instead",
    "overdraft_repayment": "Fuliza repayment - the purchase it paid for is counted instead",
    "loan_ledger": "Loan ledger entry - counted once on your savings ledger",
}

# How a bank's name shows on an M-Pesa Paybill row.
_BANK_NAMES = {
    NCBA: re.compile(r"\bNCBA\b"),
    EQUITY: re.compile(r"\bEQUITY\b"),
    MENTOR: re.compile(r"\bMENTOR\b"),
}


@dataclass(frozen=True)
class Handling:
    kind: str  # 'charge' | 'loan' | 'transfer'
    reason: str
    category: str | None = None  # default category name to file the row under
    target_provider: str | None = None  # transfer: the provider of the other leg


def _transfer(direction: str, target: str) -> Handling:
    reason = f"Sent to your {target} account" if direction == "out" else f"Received from your {target} account"
    return Handling(kind="transfer", reason=reason, target_provider=target)


def classify_row(
    *, provider: str, description: str | None, direction: str, key: RowKey, owned_providers: frozenset[str] | set[str]
) -> Handling | None:
    if key.is_person:
        return None
    if key.row_kind in _LOAN_REASONS:
        return Handling(kind="loan", reason=_LOAN_REASONS[key.row_kind])
    if key.is_loan_repayment and direction == "out":
        return Handling(kind="loan", reason="Loan repayment", category=LOAN_REPAYMENT_CATEGORY)
    if key.is_charge and direction == "out":
        text = normalize_text(description)
        reason = "Excise duty on a charge" if "EXCISE" in text else f"{provider} charge"
        return Handling(kind="charge", reason=reason, category=CHARGES_CATEGORY)
    if key.is_transfer_hint and provider == NCBA:
        return _transfer(direction, MPESA)
    if provider == MPESA and key.number_kind == "paybill" and key.name:
        for bank, pattern in _BANK_NAMES.items():
            if bank in owned_providers and pattern.search(key.name):
                return _transfer(direction, bank)
    return None


# --- pairing the two legs of a transfer across statements ----------------

PAIR_WINDOW_DAYS = 2


def plan_pairs(legs: list[dict], candidates: list[dict]) -> list[tuple[dict, dict]]:
    """Pairs each waiting transfer leg with one row on another of the
    user's accounts: at the leg's target provider, the other direction,
    the same amount, within PAIR_WINDOW_DAYS. A candidate is either another
    waiting leg (its own target must be this leg's provider) or a row
    nobody has handled or categorised yet. Closest date wins, then a
    waiting leg over an ordinary row; each row pairs at most once.

    legs/candidates: dicts with id, account_id, provider, direction,
    amount, txn_date; legs also target_provider; candidates also
    handled_as and (for legs) target_provider."""
    used: set = set()
    pairs = []
    for leg in sorted(legs, key=lambda r: (r["txn_date"], str(r["id"]))):
        if leg["id"] in used:
            continue
        options = []
        for c in candidates:
            if c["id"] in used or c["id"] == leg["id"]:
                continue
            if c["provider"] != leg["target_provider"] or c["account_id"] == leg["account_id"]:
                continue
            if c["direction"] == leg["direction"] or float(c["amount"]) != float(leg["amount"]):
                continue
            gap = abs((c["txn_date"] - leg["txn_date"]).days)
            if gap > PAIR_WINDOW_DAYS:
                continue
            if c.get("handled_as") == "transfer" and c.get("target_provider") not in (None, leg["provider"]):
                continue
            options.append((gap, 0 if c.get("handled_as") == "transfer" else 1, c["txn_date"], str(c["id"]), c))
        if not options:
            continue
        other = min(options, key=lambda o: o[:4])[4]
        used.update((leg["id"], other["id"]))
        pairs.append((leg, other))
    return pairs
