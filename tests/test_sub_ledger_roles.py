import pytest

from app.services.sub_ledger_roles import infer_role


@pytest.mark.parametrize(
    ("name", "treatment", "role"),
    [
        ("Ordinary Deposit", "addition", "locked_saving"),
        ("Non-Withdrawable Deposits", "addition", "locked_saving"),
        ("BOSA Deposits", "addition", "locked_saving"),
        ("Share Capital", "addition", "shares"),
        ("Savings Account", "addition", "spending"),
        ("Savings Deposit Account", "addition", "spending"),
        ("Instant Loan", "deduction", "loan"),
        ("Development Loan", "addition", "loan"),
        ("Holiday fund", "addition", "spending"),
    ],
)
def test_infer_role(name, treatment, role):
    assert infer_role(name, treatment) == role
