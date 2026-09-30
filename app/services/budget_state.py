from decimal import Decimal

DEFAULT_NEAR_THRESHOLD = Decimal("0.8")

# Expense categories escalate toward a warning as consumption approaches,
# then passes, the limit. Income categories are the opposite: exceeding
# the target is the GOOD outcome, not an alarm - see docs/schema.sql's
# `budgets` table comment. The two category types intentionally return a
# disjoint set of state strings so the frontend never has to guess which
# meaning a shared state (e.g. "over") has.
ExpenseState = str  # "ok" | "near" | "at" | "over"
IncomeState = str  # "on_track" | "good"


def derive_budget_state(
    *,
    category_type: str,
    limit_amount: Decimal,
    consumed: Decimal,
    near_threshold: Decimal = DEFAULT_NEAR_THRESHOLD,
) -> dict:
    """Derives a budget row's remaining amount, consumed ratio, and state.

    Never stored - recomputed from the live `consumed` sum on every read
    (see docs/schema.sql's `budgets` table comment: remaining is derived,
    not a column). `consumed` is the caller's job to compute (sum of
    allocations in this category whose transaction date falls in the
    plan's window, in the plan's own currency).
    """
    remaining = limit_amount - consumed
    # A zero limit has no meaningful ratio - any consumption at all is past
    # it (expense: "over"; income: "good", the target was trivially met),
    # none at all is the floor of the normal scale.
    if limit_amount <= 0:
        ratio = Decimal(2) if consumed > 0 else Decimal(0)
    else:
        ratio = consumed / limit_amount

    if category_type == "income":
        state: str = "good" if ratio >= 1 else "on_track"
    else:
        if ratio > 1:
            state = "over"
        elif ratio == 1:
            state = "at"
        elif ratio >= near_threshold:
            state = "near"
        else:
            state = "ok"

    # quantize, not normalize: Decimal(0).normalize() still renders as the
    # scientific "0E+2" once multiplied by 100 - quantizing to a fixed
    # 2-place scale keeps every response a plain "0.00"/"82.00"/"120.00".
    percent = (ratio * 100).quantize(Decimal("0.01"))

    return {
        "consumed": consumed,
        "remaining": remaining,
        "percent": percent,
        "state": state,
    }
