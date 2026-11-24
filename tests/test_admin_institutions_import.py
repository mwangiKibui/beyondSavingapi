"""ab-294: admin CSV import of institutions - preview, then confirm.
Repositories mocked; the SQL is checked in the live run."""

from contextlib import asynccontextmanager
from unittest.mock import AsyncMock
from uuid import uuid4

import pytest
from fastapi.testclient import TestClient

from app.core.db import get_pool
from app.core.security import get_current_user_id
from app.main import app
from app.repositories.institutions import normalize_name

API = "app.api.admin_institutions"
PREVIEW = "/admin/institutions/import/preview"
IMPORT = "/admin/institutions/import"


class FakeConn:
    def __init__(self, log):
        self.log = log

    @asynccontextmanager
    async def transaction(self):
        self.log.append("begin")
        try:
            yield
        except Exception:
            self.log.append("rollback")
            raise
        self.log.append("commit")


class FakePool:
    def __init__(self):
        self.log = []

    @asynccontextmanager
    async def acquire(self):
        yield FakeConn(self.log)


@pytest.fixture(scope="module")
def client():
    with TestClient(app) as c:
        yield c


@pytest.fixture
def pool():
    fake = FakePool()
    app.dependency_overrides[get_pool] = lambda: fake
    yield fake
    app.dependency_overrides.pop(get_pool, None)


def _as(monkeypatch, role):
    user_id = uuid4()
    app.dependency_overrides[get_current_user_id] = lambda: user_id
    monkeypatch.setattr("app.core.security.get_user_by_id", AsyncMock(return_value={"id": user_id, "role": role}))
    return user_id


@pytest.fixture
def admin(monkeypatch, pool):
    yield _as(monkeypatch, "admin")
    app.dependency_overrides.pop(get_current_user_id, None)


# Already in the table: Equity Bank (active), Biashara Sacco (retired).
EXISTING = {
    (normalize_name("Equity Bank"), "bank"): False,
    (normalize_name("Biashara Sacco"), "sacco"): True,
}


@pytest.fixture
def repo(monkeypatch):
    async def existing(conn, *, keys):
        return {k: v for k, v in EXISTING.items() if k in keys}

    mocks = {
        "existing_normalized": AsyncMock(side_effect=existing),
        "insert_institutions": AsyncMock(side_effect=lambda conn, *, rows: len(rows)),
    }
    for name, mock in mocks.items():
        monkeypatch.setattr(f"{API}.{name}", mock)
    return mocks


def upload(client, url, text, *, filename="institutions.csv", raw=None):
    content = raw if raw is not None else text.encode("utf-8")
    return client.post(url, files={"file": (filename, content, "text/csv")})


CSV = "\n".join([
    "name,account_type,regulator",
    "Sample Bank,bank,CBK",                  # 2 add
    "  equity   bank ,bank,",                # 3 exists (case + spacing)
    ",bank,CBK",                             # 4 invalid: empty name
    "Sample Wallet,crypto,",                 # 5 invalid: unknown type
    "Sample Sacco,sacco,XYZ",                # 6 invalid: bad regulator
    "Sample  bank,bank,CBK",                 # 7 invalid: repeated in file
    "Biashara Sacco,sacco,SASRA",            # 8 exists, retired
    "Sample Fund,money_market,cma",          # 9 add (regulator case)
]) + "\n"


def by_row(rows):
    return {r["row"]: r for r in rows}


def test_preview_classifies_each_row(client, admin, repo):
    r = upload(client, PREVIEW, CSV)
    assert r.status_code == 200, r.text
    body = r.json()
    rows = by_row(body["rows"])
    assert rows[2] == {"row": 2, "name": "Sample Bank", "account_type": "bank", "regulator": "CBK",
                       "status": "add", "reason": None}
    assert rows[3]["status"] == "exists" and rows[3]["reason"] == "Already there"
    assert rows[3]["name"] == "equity bank"
    assert rows[4]["status"] == "invalid" and rows[4]["reason"] == "Name can't be blank"
    assert rows[5]["status"] == "invalid" and "type" in rows[5]["reason"].lower()
    assert rows[6]["status"] == "invalid" and "regulator" in rows[6]["reason"].lower()
    assert rows[7]["status"] == "invalid" and rows[7]["reason"] == "Repeated in this file"
    assert rows[8]["status"] == "exists" and rows[8]["reason"] == "Already there (retired - restore it)"
    assert rows[9]["status"] == "add" and rows[9]["regulator"] == "CMA"
    assert (body["add"], body["exists"], body["invalid"]) == (2, 2, 4)
    repo["insert_institutions"].assert_not_awaited()


def test_preview_reads_headers_in_any_case_and_a_bom(client, admin, repo):
    text = "﻿Name,Account_Type,REGULATOR\nSample Bank,bank,CBK\n"
    r = upload(client, PREVIEW, "", raw=text.encode("utf-8"))
    assert r.status_code == 200, r.text
    assert r.json()["rows"][0]["name"] == "Sample Bank"
    assert r.json()["add"] == 1


def test_preview_reads_spreadsheet_style_headers(client, admin, repo):
    r = upload(client, PREVIEW, "Name,Account Type,Regulator\nSample Bank,bank,CBK\n")
    assert r.status_code == 200, r.text
    assert r.json()["add"] == 1


def test_missing_headers_is_422(client, admin, repo):
    r = upload(client, PREVIEW, "name,type\nSample Bank,bank\n")
    assert r.status_code == 422
    assert "account_type" in r.json()["detail"]


def test_too_many_rows_is_422(client, admin, repo):
    text = "name,account_type,regulator\n" + "".join(f"Bank {i},bank,\n" for i in range(1001))
    r = upload(client, PREVIEW, text)
    assert r.status_code == 422
    assert "1000" in r.json()["detail"]


def test_too_big_is_422(client, admin, repo):
    text = "name,account_type,regulator\n" + "x" * (201 * 1024)
    assert upload(client, PREVIEW, text).status_code == 422


def test_not_utf8_is_422(client, admin, repo):
    r = upload(client, PREVIEW, "", raw="name,account_type,regulator\nBanc\xe9,bank,\n".encode("latin-1"))
    assert r.status_code == 422
    assert r.json()["detail"] == "Save the file as CSV UTF-8"


def test_not_a_csv_is_422(client, admin, repo):
    assert upload(client, PREVIEW, "name,account_type,regulator\n", filename="list.xlsx").status_code == 422


def test_non_admin_is_forbidden(client, pool, monkeypatch, repo):
    _as(monkeypatch, "user")
    try:
        assert upload(client, PREVIEW, CSV).status_code == 403
        assert upload(client, IMPORT, CSV).status_code == 403
    finally:
        app.dependency_overrides.pop(get_current_user_id, None)
    repo["insert_institutions"].assert_not_awaited()


def test_requires_login(client, pool):
    assert upload(client, PREVIEW, CSV).status_code == 401


def test_import_inserts_only_the_add_rows_in_one_transaction(client, admin, repo, pool):
    r = upload(client, IMPORT, CSV)
    assert r.status_code == 200, r.text
    rows = repo["insert_institutions"].await_args.kwargs["rows"]
    assert rows == [
        {"name": "Sample Bank", "type": "bank", "regulator": "CBK"},
        {"name": "Sample Fund", "type": "money_market", "regulator": "CMA"},
    ]
    assert pool.log == ["begin", "commit"]
    body = r.json()
    assert (body["added"], body["exists"], body["invalid"]) == (2, 2, 4)


def test_importing_the_same_file_again_adds_nothing(client, admin, repo, monkeypatch):
    EXISTING_AFTER = {**EXISTING, (normalize_name("Sample Bank"), "bank"): False,
                      (normalize_name("Sample Fund"), "money_market"): False}

    async def existing(conn, *, keys):
        return {k: v for k, v in EXISTING_AFTER.items() if k in keys}

    repo["existing_normalized"].side_effect = existing
    r = upload(client, IMPORT, CSV)
    assert r.status_code == 200
    assert r.json()["added"] == 0
    repo["insert_institutions"].assert_not_awaited()


def test_import_db_error_is_generic_500(client, admin, repo):
    repo["insert_institutions"].side_effect = RuntimeError("boom")
    r = upload(client, IMPORT, CSV)
    assert r.status_code == 500
    assert r.json()["detail"] == "Something went wrong. Please try again."
