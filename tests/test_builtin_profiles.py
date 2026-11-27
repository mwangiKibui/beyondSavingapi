"""ab-313: built-in profiles are loaded into statement_profiles at startup -
validated, idempotent, and replaced by a new version when the JSON changes."""

import logging
from contextlib import asynccontextmanager
from unittest.mock import AsyncMock
from uuid import uuid4

import pytest

from app.parsers.profiles.builtin import BuiltinProfile, load_builtin_profiles
from app.parsers.profiles.schema import validate_profile
from app.services import statement_profiles as svc

S = "app.services.statement_profiles"
MPESA = next(b for b in load_builtin_profiles() if b.key == "mpesa")
CLEAN = validate_profile(MPESA.profile).model_dump(mode="json")


class Conn:
    def __init__(self):
        self.execute = AsyncMock()

    @asynccontextmanager
    async def transaction(self):
        yield


class Pool:
    def __init__(self):
        self.conn = Conn()

    @asynccontextmanager
    async def acquire(self):
        yield self.conn


@pytest.fixture
def repo(monkeypatch):
    m = {
        "institution": AsyncMock(return_value=uuid4()),
        "builtin": AsyncMock(return_value=[]),
        "create": AsyncMock(return_value={"id": uuid4(), "version": 1}),
        "retire": AsyncMock(),
    }
    monkeypatch.setattr(f"{S}.find_institution_id", m["institution"])
    monkeypatch.setattr(f"{S}.list_builtin_profiles", m["builtin"])
    monkeypatch.setattr(f"{S}.create_profile", m["create"])
    monkeypatch.setattr(f"{S}.retire_profiles", m["retire"])
    monkeypatch.setattr(f"{S}.load_builtin_profiles", lambda: [MPESA])
    return m


async def test_a_missing_builtin_is_created_active(repo):
    pool = Pool()
    await svc.ensure_builtin_profiles(pool)
    kwargs = repo["create"].await_args.kwargs
    assert kwargs["source"] == "builtin" and kwargs["status"] == "active"
    assert kwargs["fingerprint"] == MPESA.fingerprint and kwargs["profile"] == MPESA.profile
    repo["retire"].assert_not_awaited()
    # One instance at a time when several start together.
    assert "pg_advisory_xact_lock" in pool.conn.execute.await_args.args[0]


async def test_an_unchanged_builtin_is_left_alone(repo):
    repo["builtin"].return_value = [{"id": uuid4(), "fingerprint": MPESA.fingerprint, "profile": CLEAN}]
    await svc.ensure_builtin_profiles(Pool())
    repo["create"].assert_not_awaited()
    repo["retire"].assert_not_awaited()


async def test_a_builtin_an_admin_retired_stays_retired(repo):
    """ab-317: the startup sync doesn't bring back what an admin retired."""
    repo["builtin"].return_value = [
        {"id": uuid4(), "fingerprint": MPESA.fingerprint, "profile": CLEAN, "status": "retired"}]
    await svc.ensure_builtin_profiles(Pool())
    repo["create"].assert_not_awaited()
    repo["retire"].assert_not_awaited()


async def test_a_changed_builtin_retires_only_the_active_old_one(repo):
    active = {"id": uuid4(), "fingerprint": MPESA.fingerprint, "profile": {**CLEAN, "currency": None}, "status": "active"}
    retired = {"id": uuid4(), "fingerprint": MPESA.fingerprint, "profile": {**CLEAN, "currency": "USD"}, "status": "retired"}
    repo["builtin"].return_value = [active, retired]
    await svc.ensure_builtin_profiles(Pool())
    repo["create"].assert_awaited_once()
    assert repo["retire"].await_args.kwargs["profile_ids"] == [active["id"]]


async def test_a_changed_builtin_becomes_a_new_version_and_retires_the_old(repo):
    old = {"id": uuid4(), "fingerprint": MPESA.fingerprint, "profile": {**CLEAN, "currency": None}}
    repo["builtin"].return_value = [old]
    await svc.ensure_builtin_profiles(Pool())
    repo["create"].assert_awaited_once()
    assert repo["retire"].await_args.kwargs["profile_ids"] == [old["id"]]


async def test_an_institution_missing_from_the_table_is_skipped(repo, caplog):
    repo["institution"].return_value = None
    with caplog.at_level(logging.WARNING):
        await svc.ensure_builtin_profiles(Pool())
    repo["create"].assert_not_awaited()
    assert any("M-Pesa" in r.message for r in caplog.records)


async def test_an_invalid_builtin_is_skipped_and_the_rest_still_load(repo, monkeypatch, caplog):
    broken = BuiltinProfile(key="broken", institution_name="X", institution_type="bank", fingerprint="{}", profile={})
    monkeypatch.setattr(f"{S}.load_builtin_profiles", lambda: [broken, MPESA])
    with caplog.at_level(logging.ERROR):
        await svc.ensure_builtin_profiles(Pool())
    assert repo["create"].await_count == 1
    assert any("broken" in r.message for r in caplog.records)


async def test_a_database_error_never_raises(repo):
    repo["builtin"].side_effect = RuntimeError("db down")
    await svc.ensure_builtin_profiles(Pool())  # logged, not raised
