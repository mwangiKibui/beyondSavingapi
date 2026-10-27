from decimal import Decimal

from app.services.money_plan_summary import summarise_money_plan


def test_empty_plan_is_all_zero_with_no_save_rate():
    result = summarise_money_plan(
        income=Decimal("0"), spending=Decimal("0"), loan_repayments=Decimal("0"), savings=Decimal("0")
    )
    assert result["left_over"] == Decimal("0")
    assert result["will_save"] == Decimal("0")
    assert result["save_rate"] is None


def test_typical_month():
    result = summarise_money_plan(
        income=Decimal("80000"),
        spending=Decimal("50000"),
        loan_repayments=Decimal("10000"),
        savings=Decimal("8000"),
    )
    assert result["income"] == Decimal("80000")
    assert result["left_over"] == Decimal("12000")
    # savings + left over
    assert result["will_save"] == Decimal("20000")
    assert result["save_rate"] == Decimal("25.00")


def test_negative_left_over_is_kept_but_not_counted_as_saving():
    result = summarise_money_plan(
        income=Decimal("30000"),
        spending=Decimal("28000"),
        loan_repayments=Decimal("3000"),
        savings=Decimal("2000"),
    )
    assert result["left_over"] == Decimal("-3000")
    assert result["will_save"] == Decimal("2000")
    assert result["save_rate"] == Decimal("6.67")


def test_no_income_has_no_save_rate():
    result = summarise_money_plan(
        income=Decimal("0"), spending=Decimal("500"), loan_repayments=Decimal("0"), savings=Decimal("1000")
    )
    assert result["left_over"] == Decimal("-1500")
    assert result["will_save"] == Decimal("1000")
    assert result["save_rate"] is None
