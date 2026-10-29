from unittest.mock import AsyncMock
from uuid import uuid4

import pytest
from fastapi.testclient import TestClient

from app.core.db import get_pool
from app.core.security import get_current_user_id
from app.main import app


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


def test_delete_money_plan_requires_auth(client):
    response = client.delete(f"/money-plans/{uuid4()}")
    assert response.status_code == 401


def test_delete_money_plan_returns_503_when_db_unreachable(client, fake_user):
    response = client.delete(f"/money-plans/{uuid4()}")
    assert response.status_code == 503


def test_delete_money_plan_rejects_a_non_uuid_id(client, fake_user, fake_pool):
    response = client.delete("/money-plans/not-a-uuid")
    assert response.status_code == 422


def test_delete_money_plan_deletes_a_draft(client, fake_user, fake_pool, monkeypatch):
    plan_id = uuid4()
    mock_delete = AsyncMock(return_value="deleted")
    monkeypatch.setattr("app.api.money_plans.delete_draft_money_plan", mock_delete)

    response = client.delete(f"/money-plans/{plan_id}")

    assert response.status_code == 204
    assert response.content == b""
    mock_delete.assert_awaited_once()
    assert mock_delete.await_args.kwargs == {"user_id": fake_user, "plan_id": plan_id}


def test_delete_money_plan_returns_404_for_a_missing_or_other_users_plan(
    client, fake_user, fake_pool, monkeypatch
):
    monkeypatch.setattr(
        "app.api.money_plans.delete_draft_money_plan", AsyncMock(return_value="not_found")
    )

    response = client.delete(f"/money-plans/{uuid4()}")

    assert response.status_code == 404
    assert response.json()["detail"] == "Money plan not found"


def test_delete_money_plan_returns_409_for_a_plan_that_is_not_a_draft(
    client, fake_user, fake_pool, monkeypatch
):
    monkeypatch.setattr(
        "app.api.money_plans.delete_draft_money_plan", AsyncMock(return_value="not_draft")
    )

    response = client.delete(f"/money-plans/{uuid4()}")

    assert response.status_code == 409
    assert response.json()["detail"] == "Only draft plans can be deleted"


def test_delete_money_plan_returns_generic_500_and_logs_on_unexpected_error(
    client, fake_user, fake_pool, monkeypatch, caplog
):
    monkeypatch.setattr(
        "app.api.money_plans.delete_draft_money_plan",
        AsyncMock(side_effect=RuntimeError("connection reset")),
    )

    with caplog.at_level("ERROR"):
        response = client.delete(f"/money-plans/{uuid4()}")

    assert response.status_code == 500
    assert response.json()["detail"] == "Something went wrong. Please try again."
    assert "connection reset" not in response.text
    assert "Unexpected error deleting money plan" in caplog.text
