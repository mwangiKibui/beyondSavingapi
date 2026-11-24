"""ab-294: GET /institutions - the active providers the account pickers
offer. Repository mocked; the SQL is checked in the live run."""

import logging
from contextlib import asynccontextmanager
from unittest.mock import AsyncMock
from uuid import uuid4

import pytest
from fastapi.testclient import TestClient

from app.core.db import get_pool
from app.core.security import get_current_user_id
from app.main import app

API = "app.api.institutions"


class FakePool:
    @asynccontextmanager
    async def acquire(self):
        yield object()


@pytest.fixture(scope="module")
def client():
    with TestClient(app) as c:
        yield c


@pytest.fixture
def pool():
    app.dependency_overrides[get_pool] = lambda: FakePool()
    yield
    app.dependency_overrides.pop(get_pool, None)


@pytest.fixture
def user():
    user_id = uuid4()
    app.dependency_overrides[get_current_user_id] = lambda: user_id
    yield user_id
    app.dependency_overrides.pop(get_current_user_id, None)


def provider(name, type, uploadable=False, recognised=False):
    return {
        "id": uuid4(), "name": name, "type": type,
        "statements_uploadable": uploadable, "recognised_layout": recognised,
    }


@pytest.fixture
def list_active(monkeypatch):
    mock = AsyncMock(return_value=[provider("Equity Bank", "bank", True), provider("M-Pesa", "mobile_money", True)])
    monkeypatch.setattr(f"{API}.list_active", mock)
    return mock


def test_requires_login(client, pool):
    assert client.get("/institutions").status_code == 401


def test_lists_active_providers(client, user, pool, list_active):
    r = client.get("/institutions")
    assert r.status_code == 200
    items = r.json()["items"]
    assert [i["name"] for i in items] == ["Equity Bank", "M-Pesa"]
    assert set(items[0]) == {"id", "name", "type", "statements_uploadable", "recognised_layout"}
    assert items[0]["recognised_layout"] is False
    assert items[0]["statements_uploadable"] is True
    assert list_active.await_args.kwargs == {"type": None}


def test_filters_by_account_type(client, user, pool, list_active):
    assert client.get("/institutions", params={"account_type": "sacco"}).status_code == 200
    assert list_active.await_args.kwargs == {"type": "sacco"}


@pytest.mark.parametrize("bad", ["insurer", "crypto"])
def test_rejects_types_accounts_cant_have(client, user, pool, list_active, bad):
    assert client.get("/institutions", params={"account_type": bad}).status_code == 422
    list_active.assert_not_awaited()


def test_db_error_is_generic_500(client, user, pool, monkeypatch, caplog):
    monkeypatch.setattr(f"{API}.list_active", AsyncMock(side_effect=RuntimeError("boom secret=xyz")))
    with caplog.at_level(logging.ERROR):
        r = client.get("/institutions")
    assert r.status_code == 500
    assert r.json()["detail"] == "Something went wrong. Please try again."
    assert any(rec.exc_info for rec in caplog.records if rec.levelno == logging.ERROR)


def test_no_pool_is_503(client, user):
    assert client.get("/institutions").status_code == 503
