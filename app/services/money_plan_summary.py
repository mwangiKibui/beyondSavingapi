from decimal import ROUND_HALF_UP, Decimal

_ZERO = Decimal("0")
_CENTS = Decimal("0.01")


def summarise_money_plan(
    *,
    income: Decimal,
    spending: Decimal,
    loan_repayments: Decimal,
    savings: Decimal,
) -> dict:
    """Derives a money plan's bottom line from its per-type line totals.

    Never stored - recomputed on every read, and the planner's live footer
    (ab-175) uses these exact formulas so the two always agree:

    - left_over = income - spending - loan repayments - savings. Money not
      assigned to anything; may be negative (shown neutrally, never as a
      warning).
    - will_save = savings + left_over, with a negative left_over counting
      as zero - an overspend doesn't cancel out the savings already planned.
    - save_rate = will_save as a percentage of income, 2dp. None when there
      is no income, since there's nothing to take a share of.
    """
    left_over = income - spending - loan_repayments - savings
    will_save = savings + max(left_over, _ZERO)
    save_rate = None
    if income > _ZERO:
        save_rate = (will_save / income * 100).quantize(_CENTS, rounding=ROUND_HALF_UP)

    return {
        "income": income,
        "spending": spending,
        "loan_repayments": loan_repayments,
        "savings": savings,
        "left_over": left_over,
        "will_save": will_save,
        "save_rate": save_rate,
    }
