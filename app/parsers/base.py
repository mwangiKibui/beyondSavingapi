from datetime import date
from typing import Literal, Protocol, TypedDict


class ParsedTransaction(TypedDict):
    """One row a parser extracts from a statement file.

    Mirrors the transactions table's own columns (docs/schema.sql) so a
    parser's output can be inserted with no further shape translation,
    once ab-44 (write parsed transactions + update import status) exists
    to do that insert.
    """

    txn_date: date
    amount: float
    currency: str
    direction: Literal["in", "out"]
    counterparty: str | None
    description: str | None
    balance_after: float | None
    # Not a transactions column itself - written into the dedupe_hash
    # column by ab-44/ab-43. Only the parser understands its own
    # provider's reference-number format well enough to build a stable
    # fingerprint (see docs/provider-onboarding-runbook.md's per-provider
    # "Dedupe strategy" entries), so it's computed here rather than
    # downstream from generic fields alone.
    dedupe_hash: str


class StatementParser(Protocol):
    """Contract every per-provider parser (ab-39/ab-40/ab-115) must meet.

    Takes the original uploaded file's raw bytes (already password-
    unlocked - see app/services/statement_files.py) and returns every
    transaction it found, oldest first. A parser validates its own
    output against the statement's own stated opening balance before
    returning (ab-42's `validate_running_balance`, below) rather than
    surfacing that balance itself - nothing downstream of this contract
    needs it.
    """

    def __call__(self, content: bytes) -> list[ParsedTransaction]: ...


# Keyed by accounts.provider (see app/api/accounts.py's PROVIDERS_BY_TYPE
# for the fixed set of MVP1 providers). Empty until ab-39/ab-40/ab-115
# register a real parser for their provider(s).
PARSERS: dict[str, StatementParser] = {}


class StatementParseError(Exception):
    """A parser can't produce a trustworthy result - a required anchor
    (e.g. a stated opening balance) is missing, or a balance chain
    doesn't reconcile. Raised instead of silently guessing, so the
    worker (app/worker.py) can mark the import failed with a clear
    reason rather than persisting a wrong result once ab-44 exists.
    """


class RunningBalanceMismatch(StatementParseError):
    """A parsed statement's running balance didn't reconcile - replaying
    from the opening balance didn't match some row's own stated
    balance_after. Signals corrupted extraction, a bad row order, or a
    provider format that isn't correctly modeled yet.
    """


def validate_running_balance(transactions: list[ParsedTransaction], *, opening_balance: float) -> None:
    """Replays `transactions` (oldest first) against `opening_balance`
    and confirms each row's own stated `balance_after` reconciles.

    Only meaningful when at least one of amount/direction/balance_after
    is independently stated rather than derived from the others (see
    each parser's own comments) - see ab-42's ticket notes for why this
    is applied to the whole output for Equity Bank/NCBA/Mentor Sacco but
    only to same-timestamp clusters for M-Pesa (its "Balance" column
    isn't one continuous ledger across Fuliza-related entries).
    """
    previous_balance = opening_balance
    for i, txn in enumerate(transactions):
        expected = round(
            previous_balance + txn["amount"] if txn["direction"] == "in" else previous_balance - txn["amount"],
            2,
        )
        if abs(expected - txn["balance_after"]) > 0.01:
            raise RunningBalanceMismatch(
                f"Running balance mismatch at transaction {i + 1} ({txn['description'] or 'no description'}): "
                f"expected {expected:.2f}, statement shows {txn['balance_after']:.2f}"
            )
        previous_balance = txn["balance_after"]


def statement_period(transactions: list[ParsedTransaction]) -> tuple[date, date]:
    """The date range a batch of parsed transactions covers - written
    onto statement_imports.period_start/period_end once ab-44 flips an
    import to 'parsed'. Callers only call this once they have at least
    one transaction (an import with zero rows has nothing to derive a
    period from, and isn't this function's job to special-case).
    """
    dates = [txn["txn_date"] for txn in transactions]
    return min(dates), max(dates)
