from decimal import Decimal

from app.services.budget_state import derive_budget_state


def test_expense_below_near_threshold_is_ok():
    result = derive_budget_state(category_type="expense", limit_amount=Decimal("1000"), consumed=Decimal("400"))
    assert result["state"] == "ok"
    assert result["remaining"] == Decimal("600")
    assert result["percent"] == Decimal("40")


def test_expense_at_or_above_near_threshold_is_near():
    result = derive_budget_state(category_type="expense", limit_amount=Decimal("1000"), consumed=Decimal("820"))
    assert result["state"] == "near"


def test_expense_exactly_at_limit_is_at():
    result = derive_budget_state(category_type="expense", limit_amount=Decimal("1000"), consumed=Decimal("1000"))
    assert result["state"] == "at"
    assert result["remaining"] == Decimal("0")


def test_expense_past_limit_is_over():
    result = derive_budget_state(category_type="expense", limit_amount=Decimal("500"), consumed=Decimal("600"))
    assert result["state"] == "over"
    assert result["remaining"] == Decimal("-100")
    assert result["percent"] == Decimal("120")


def test_income_below_target_is_on_track():
    result = derive_budget_state(category_type="income", limit_amount=Decimal("10000"), consumed=Decimal("7000"))
    assert result["state"] == "on_track"
    assert result["remaining"] == Decimal("3000")


def test_income_meeting_or_exceeding_target_is_good():
    result = derive_budget_state(category_type="income", limit_amount=Decimal("10000"), consumed=Decimal("10500"))
    assert result["state"] == "good"
    assert result["remaining"] == Decimal("-500")


def test_zero_limit_with_no_consumption_is_ok():
    result = derive_budget_state(category_type="expense", limit_amount=Decimal("0"), consumed=Decimal("0"))
    assert result["state"] == "ok"


def test_zero_limit_with_any_consumption_is_over():
    result = derive_budget_state(category_type="expense", limit_amount=Decimal("0"), consumed=Decimal("50"))
    assert result["state"] == "over"


def test_custom_near_threshold_is_respected():
    result = derive_budget_state(
        category_type="expense",
        limit_amount=Decimal("1000"),
        consumed=Decimal("500"),
        near_threshold=Decimal("0.5"),
    )
    assert result["state"] == "near"


def test_uncapped_income_has_no_remaining_percent_or_state():
    result = derive_budget_state(category_type="income", limit_amount=None, consumed=Decimal("7000"))
    assert result["consumed"] == Decimal("7000")
    assert result["remaining"] is None
    assert result["percent"] is None
    assert result["state"] is None


def test_uncapped_with_zero_consumption_has_no_remaining_percent_or_state():
    result = derive_budget_state(category_type="income", limit_amount=None, consumed=Decimal("0"))
    assert result["remaining"] is None
    assert result["percent"] is None
    assert result["state"] is None
