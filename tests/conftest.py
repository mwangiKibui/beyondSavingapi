import os

import pytest

os.environ.setdefault("DATABASE_URL", "postgresql://test:test@localhost:5432/test")
os.environ.setdefault("REDIS_URL", "redis://localhost:6379/0")
os.environ.setdefault("STORAGE_ENDPOINT", "http://localhost:9000")
os.environ.setdefault("STORAGE_ACCESS_KEY", "test")
os.environ.setdefault("STORAGE_SECRET_KEY", "test")
os.environ.setdefault("JWT_SECRET", "test-secret")
os.environ.setdefault("SUGGESTION_KEY_SECRET", "test-suggestion-secret")
# ab-313: tests never write built-in profiles into whatever DB is reachable.
os.environ["SEED_BUILTIN_PROFILES"] = "false"


@pytest.fixture(autouse=True)
def _fresh_directory_cache():
    """ab-300: the payee Directory is cached per process; no test may see
    another test's."""
    from app.services import directory_cache

    directory_cache.clear()
    yield
    directory_cache.clear()


# ab-294: providers come from the institutions table. Tests that create or
# edit accounts against a fake pool use this stand-in for the 10 providers
# migration 000034 seeds; any other name passes only as the account's
# current provider (how a retired one stays editable).
SEEDED_PROVIDERS = {
    "bank": ["Equity Bank", "NCBA Bank"],
    "mobile_money": ["M-Pesa", "Airtel Money"],
    "sacco": ["Mentor Sacco", "Biashara Sacco"],
    "money_market": [
        "CIC Money Market Fund",
        "Sanlam Money Market Fund",
        "Etica Money Market Fund",
        "Old Mutual Money Market Fund",
    ],
}


@pytest.fixture
def seeded_providers(monkeypatch):
    from unittest.mock import AsyncMock

    async def provider_allowed(conn, *, name, type, current=None):
        return name in SEEDED_PROVIDERS.get(type, []) or (current is not None and name == current)

    mock = AsyncMock(side_effect=provider_allowed)
    monkeypatch.setattr("app.api.accounts.provider_allowed", mock)
    return mock


@pytest.fixture(autouse=True)
def _unlimited_plans(monkeypatch):
    """ab-309: limit checks read the user's plan from the DB. Tests run
    against fake pools, so every plan is unlimited unless a test sets a
    value (patch app.services.limits.get_plan_limit, or use the limits
    fixture)."""
    from unittest.mock import AsyncMock

    monkeypatch.setattr("app.services.limits.get_plan_limit", AsyncMock(return_value=None))


@pytest.fixture
def limits(monkeypatch):
    """ab-309: set a plan value and usage for every limit check:
    limits(value, used) - value None = unlimited."""
    from unittest.mock import AsyncMock

    def set_limit(value, used=0):
        monkeypatch.setattr("app.services.limits.get_plan_limit", AsyncMock(return_value=value))
        for name in ("count_imports_reached_allocation", "count_active_accounts"):
            monkeypatch.setattr(f"app.services.limits.{name}", AsyncMock(return_value=used))

    return set_limit
