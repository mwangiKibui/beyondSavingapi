"""ab-281: DELETE /transactions/{id}/handling and PUT .../handling/category."""

from contextlib import asynccontextmanager
from unittest.mock import AsyncMock
from uuid import uuid4

import pytest
from fastapi.testclient import TestClient

from app.core.db import get_pool
from app.core.security import get_current_user_id
from app.main import app
from app.services.import_handling import HandlingRejected

API = "app.api.transactions"
TXN_ID = uuid4()


class FakeConn:
    def transaction(self):
        @asynccontextmanager
        async def tx():
            yield
        return tx()


class FakePool:
    def acquire(self):
        @asynccontextmanager
        async def acquire():
            yield FakeConn()
        return acquire()


@pytest.fixture(scope="module")
def client():
    with TestClient(app) as c:
        yield c


@pytest.fixture
def fake_pool():
    app.dependency_overrides[get_pool] = lambda: FakePool()
    yield
    app.dependency_overrides.pop(get_pool, None)


@pytest.fixture
def fake_user():
    user_id = uuid4()
    app.dependency_overrides[get_current_user_id] = lambda: user_id
    yield user_id
    app.dependency_overrides.pop(get_current_user_id, None)


def test_handling_endpoints_require_auth(client):
    assert client.delete(f"/transactions/{TXN_ID}/handling").status_code == 401
    assert client.put(f"/transactions/{TXN_ID}/handling/category", json={"category_id": str(uuid4())}).status_code == 401


def test_undo_returns_the_rows_back_in_review(client, fake_user, fake_pool, monkeypatch):
    other = uuid4()
    undo = AsyncMock(return_value=[TXN_ID, other])
    monkeypatch.setattr(f"{API}.undo_handling", undo)

    response = client.delete(f"/transactions/{TXN_ID}/handling")

    assert response.status_code == 200
    assert response.json() == {"returned_to_review": [str(TXN_ID), str(other)]}
    assert undo.call_args.kwargs["transaction_id"] == TXN_ID and undo.call_args.kwargs["user_id"] == fake_user


@pytest.mark.parametrize("code", [404, 409])
def test_undo_rejections_pass_through(client, fake_user, fake_pool, monkeypatch, code):
    monkeypatch.setattr(f"{API}.undo_handling", AsyncMock(side_effect=HandlingRejected(code, "nope")))
    response = client.delete(f"/transactions/{TXN_ID}/handling")
    assert response.status_code == code and response.json()["detail"] == "nope"


def test_unexpected_errors_are_generic(client, fake_user, fake_pool, monkeypatch):
    monkeypatch.setattr(f"{API}.undo_handling", AsyncMock(side_effect=RuntimeError("relation does not exist")))
    response = client.delete(f"/transactions/{TXN_ID}/handling")
    assert response.status_code == 500 and "relation" not in response.text


def test_undo_returns_503_when_db_unreachable(client, fake_user):
    assert client.delete(f"/transactions/{TXN_ID}/handling").status_code == 503


def test_change_category(client, fake_user, fake_pool, monkeypatch):
    category_id = uuid4()
    change = AsyncMock(return_value={"transaction_id": TXN_ID, "category_id": category_id, "category_name": "Fees"})
    monkeypatch.setattr(f"{API}.change_handled_category", change)

    response = client.put(f"/transactions/{TXN_ID}/handling/category", json={"category_id": str(category_id)})

    assert response.status_code == 200
    assert response.json()["category_name"] == "Fees"
    assert change.call_args.kwargs["category_id"] == category_id


def test_change_category_validates_the_body(client, fake_user, fake_pool):
    assert client.put(f"/transactions/{TXN_ID}/handling/category", json={}).status_code == 422
