"""ab-320: the bespoke PDF parsers are retired - built-in profiles read
M-Pesa, Equity Bank and NCBA Bank, and app/ never imports the references."""

import subprocess
from pathlib import Path

from app.parsers import PARSERS, STATEMENT_PROVIDERS


def test_no_bespoke_parser_is_registered():
    assert PARSERS == {}


def test_the_retired_providers_stay_uploadable():
    assert {"M-Pesa", "Equity Bank", "NCBA Bank", "Mentor Sacco"} <= STATEMENT_PROVIDERS


def test_app_never_imports_the_golden_references():
    app = Path(__file__).parent.parent / "app"
    found = subprocess.run(["grep", "-rnE", "(from|import) tests", str(app)], capture_output=True, text=True).stdout
    assert found == ""
