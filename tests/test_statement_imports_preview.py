"""ab-253: GET /statement-imports/{id}/preview - the import's open rows
with category suggestions. Read-only."""

from datetime import date, datetime, timezone
from unittest.mock import AsyncMock
from uuid import uuid4

import pytest
from fastapi.testclient import TestClient

from app.core.db import get_pool
from app.core.security import get_current_user_id
from app.main import app

API = "app.api.statement_imports"
IMPORT_ID = uuid4()
GROCERIES = {"id": uuid4(), "name": "Groceries", "type": "expense"}


@pytest.fixture(scope="module")
def client():
    with TestClient(app) as c:
        yield c


@pytest.fixture
def fake_pool():
    app.dependency_overrides[get_pool] = lambda: object()
    yield
    app.dependency_overrides.pop(get_pool, None)


@pytest.fixture
def fake_user():
    user_id = uuid4()
    app.dependency_overrides[get_current_user_id] = lambda: user_id
    yield user_id
    app.dependency_overrides.pop(get_current_user_id, None)


def import_row(status="parsed", provider="M-Pesa"):
    return {"id": IMPORT_ID, "account_id": uuid4(), "account_nickname": "My M-Pesa", "provider": provider,
            "file_name": "statement.pdf", "status": status, "period_start": date(2026, 10, 1),
            "period_end": date(2026, 10, 31), "row_count": 3, "created_at": datetime(2026, 11, 1, tzinfo=timezone.utc)}


def txn(description, *, amount=1000.0, direction="out", allocated=0, category_id=None):
    return {"id": uuid4(), "txn_date": date(2026, 10, 5), "amount": amount, "currency": "KES",
            "direction": direction, "description": description, "is_transfer": False,
            "allocated": allocated, "category_id": category_id}


def directory_row(name, category, pattern):
    return {"merchant_id": uuid4(), "display_name": name, "category_name": category, "direction": "out",
            "key_kind": "name_pattern", "key_value": pattern, "requires_text": False}


def patch_reads(monkeypatch, *, imp=None, rows=(), rules=(), directory=(), categories=(GROCERIES,),
                shared_votes=None):
    mocks = {
        "get_import_for_preview": AsyncMock(return_value=imp),
        "get_import_rows": AsyncMock(return_value=list(rows)),
        "get_user_rules": AsyncMock(return_value=list(rules)),
        "get_approved_directory": AsyncMock(return_value=list(directory)),
        "get_blocked_numbers": AsyncMock(return_value=[]),
        "get_user_categories": AsyncMock(return_value=list(categories)),
        "get_shared_votes": AsyncMock(return_value=dict(shared_votes or {})),
    }
    for name, mock in mocks.items():
        monkeypatch.setattr(f"{API}.{name}", mock)
    return mocks


def test_preview_requires_auth(client):
    assert client.get(f"/statement-imports/{IMPORT_ID}/preview").status_code == 401


def test_preview_returns_503_when_db_unreachable(client, fake_user):
    assert client.get(f"/statement-imports/{IMPORT_ID}/preview").status_code == 503


def test_preview_returns_404_for_an_unknown_or_foreign_import(client, fake_user, fake_pool, monkeypatch):
    patch_reads(monkeypatch, imp=None)
    response = client.get(f"/statement-imports/{IMPORT_ID}/preview")
    assert response.status_code == 404
    assert response.json()["detail"] == "Statement import not found"


@pytest.mark.parametrize("status", ["pending", "failed"])
def test_preview_returns_409_until_the_import_is_parsed(client, fake_user, fake_pool, monkeypatch, status):
    mocks = patch_reads(monkeypatch, imp=import_row(status=status))
    response = client.get(f"/statement-imports/{IMPORT_ID}/preview")
    assert response.status_code == 409
    mocks["get_import_rows"].assert_not_called()


def test_preview_returns_open_rows_with_suggestions(client, fake_user, fake_pool, monkeypatch):
    naivas = txn("Merchant Payment to 5555555 - NAIVAS WESTLANDS")
    shell = txn("Merchant Payment to 6666666 - SHELL RIRUTA")
    shell2 = txn("Merchant Payment to 6666666 - SHELL RIRUTA", amount=2000.0)
    unknown = txn("Merchant Payment to 7777777 - SOMEONE")
    done = txn("Merchant Payment to 8888888 - DONE LTD", allocated=1000.0, category_id=GROCERIES["id"])
    mocks = patch_reads(
        monkeypatch,
        imp=import_row(),
        rows=[naivas, shell, shell2, unknown, done],
        directory=[directory_row("Naivas", "Groceries", "NAIVAS"), directory_row("Shell", "Fuel", "SHELL")],
    )

    response = client.get(f"/statement-imports/{IMPORT_ID}/preview")

    assert response.status_code == 200
    body = response.json()
    assert body["import"]["id"] == str(IMPORT_ID)
    assert body["import"]["provider"] == "M-Pesa"
    rows = {r["transaction_id"]: r for r in body["rows"]}
    assert str(done["id"]) not in rows
    assert rows[str(naivas["id"])]["suggestion"]["category_id"] == str(GROCERIES["id"])
    assert rows[str(naivas["id"])]["suggestion"]["source"] == "directory"
    assert rows[str(shell["id"])]["suggestion"]["is_new_category"] is True
    assert rows[str(shell["id"])]["group_key"] == rows[str(shell2["id"])]["group_key"]
    assert rows[str(unknown["id"])]["suggestion"] is None
    assert body["summary"] == {"open_rows": 4, "suggested": 3, "new_categories": ["Fuel"]}

    # rules are fetched once, for the distinct keys only
    mocks["get_user_rules"].assert_awaited_once()
    pairs = mocks["get_user_rules"].call_args.kwargs["pairs"]
    assert sorted(pairs) == [("till", "5555555"), ("till", "6666666"), ("till", "7777777"), ("till", "8888888")]


def test_preview_uses_the_accounts_provider_for_parsing(client, fake_user, fake_pool, monkeypatch):
    row = txn("IB Outgoing Mobile Payments BuyGoods FTX26216ABCDE 9120385")
    patch_reads(monkeypatch, imp=import_row(provider="NCBA Bank"), rows=[row])
    body = client.get(f"/statement-imports/{IMPORT_ID}/preview").json()
    assert body["rows"][0]["display_name"] == "Till 9120385"
    assert body["rows"][0]["key_kind"] == "till"


def test_preview_returns_generic_500_on_unexpected_error(client, fake_user, fake_pool, monkeypatch):
    patch_reads(monkeypatch, imp=import_row())
    monkeypatch.setattr(f"{API}.get_import_rows", AsyncMock(side_effect=RuntimeError("db exploded")))
    response = client.get(f"/statement-imports/{IMPORT_ID}/preview")
    assert response.status_code == 500
    assert "exploded" not in response.text


def test_preview_reads_rows_by_account_and_period_so_reuploads_still_show(client, fake_user, fake_pool, monkeypatch):
    imp = import_row()
    mocks = patch_reads(monkeypatch, imp=imp)
    client.get(f"/statement-imports/{IMPORT_ID}/preview")
    mocks["get_import_rows"].assert_awaited_once()
    kwargs = mocks["get_import_rows"].call_args.kwargs
    assert kwargs == {"import_id": IMPORT_ID, "account_id": imp["account_id"],
                      "period_start": imp["period_start"], "period_end": imp["period_end"]}



def test_preview_suggests_what_most_users_chose(client, fake_user, fake_pool, monkeypatch):
    row = txn("Merchant Payment to 6123456 - RUBIS ENERGY LTD")
    mocks = patch_reads(
        monkeypatch, imp=import_row(), rows=[row],
        directory=[directory_row("Rubis", "Transport", "RUBIS")],
        shared_votes={("till", "6123456", "out"): [{"category_name": "Groceries", "users": 3}]},
    )
    body = client.get(f"/statement-imports/{IMPORT_ID}/preview").json()
    assert body["rows"][0]["suggestion"]["source"] == "others"
    assert body["rows"][0]["suggestion"]["category_id"] == str(GROCERIES["id"])
    kwargs = mocks["get_shared_votes"].call_args.kwargs
    assert kwargs["name_keys"] == [("name", "RUBIS ENERGY LTD", "out")]
    assert kwargs["number_keys"] == [("till", "6123456", "out")]



# --- preview.csv ----------------------------------------------------------

CSV_URL = f"/statement-imports/{IMPORT_ID}/preview.csv"


def test_csv_requires_auth(client):
    assert client.get(CSV_URL).status_code == 401


def test_csv_returns_503_when_db_unreachable(client, fake_user):
    assert client.get(CSV_URL).status_code == 503


def test_csv_404_and_409_like_the_preview(client, fake_user, fake_pool, monkeypatch):
    patch_reads(monkeypatch, imp=None)
    assert client.get(CSV_URL).status_code == 404
    patch_reads(monkeypatch, imp=import_row(status="pending"))
    assert client.get(CSV_URL).status_code == 409


def test_csv_download_has_every_row_and_a_safe_filename(client, fake_user, fake_pool, monkeypatch):
    imp = {**import_row(), "file_name": "NCBA Oct/2026 \"statement\".pdf"}
    open_row = {**txn("Merchant Payment to 5555555 - NAIVAS WESTLANDS"), "balance_after": 900.0,
                "category_count": 0, "has_uncategorised": False}
    done = {**txn("=SUM(1)", allocated=1000.0, category_id=GROCERIES["id"]), "balance_after": None,
            "category_count": 1, "has_uncategorised": False}
    patch_reads(monkeypatch, imp=imp, rows=[open_row, done],
                directory=[directory_row("Naivas", "Groceries", "NAIVAS")])

    response = client.get(CSV_URL)

    assert response.status_code == 200
    assert response.headers["content-type"].startswith("text/csv")
    assert response.headers["content-disposition"] == 'attachment; filename="NCBA-Oct-2026-statement-suggestions.csv"'
    lines = response.text.splitlines()
    assert lines[0].startswith("date,description,payee")
    assert len(lines) == 3
    assert "Groceries,no,directory,Naivas," in lines[1]
    assert lines[2].startswith("2026-10-05,'=SUM(1)") and lines[2].endswith(",Groceries")
