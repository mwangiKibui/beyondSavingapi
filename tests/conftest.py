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
