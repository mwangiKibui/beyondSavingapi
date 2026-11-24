import os

import pytest

os.environ.setdefault("DATABASE_URL", "postgresql://test:test@localhost:5432/test")
os.environ.setdefault("REDIS_URL", "redis://localhost:6379/0")
os.environ.setdefault("STORAGE_ENDPOINT", "http://localhost:9000")
os.environ.setdefault("STORAGE_ACCESS_KEY", "test")
os.environ.setdefault("STORAGE_SECRET_KEY", "test")
os.environ.setdefault("JWT_SECRET", "test-secret")
os.environ.setdefault("SUGGESTION_KEY_SECRET", "test-suggestion-secret")


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
