"""ab-263: storage is configured as a plain S3 endpoint, so local RustFS and
DigitalOcean Spaces (test/prod) differ only in env vars, never in code."""

import pytest

from app.core.config import Settings
from app.core.storage import build_storage_client

BASE = dict(database_url="postgresql://x", redis_url="redis://x", jwt_secret="x", suggestion_key_secret="x")


def settings(**env):
    return Settings(_env_file=None, **BASE, **env)


def test_local_defaults_point_at_the_compose_storage_service():
    s = settings(storage_access_key="k", storage_secret_key="s")
    assert (s.storage_endpoint, s.storage_region, s.storage_bucket) == ("http://localhost:9000", "us-east-1", "statements")


@pytest.mark.parametrize("endpoint,region", [
    ("http://storage:9000", "us-east-1"),
    ("https://fra1.digitaloceanspaces.com", "fra1"),
])
def test_client_uses_the_configured_endpoint_and_region(endpoint, region):
    client = build_storage_client(settings(storage_endpoint=endpoint, storage_region=region,
                                           storage_access_key="k", storage_secret_key="s"))
    assert client.meta.endpoint_url == endpoint
    assert client.meta.region_name == region
    assert client.meta.config.signature_version == "s3v4"
    assert client.meta.config.s3["addressing_style"] == "path"


def test_old_minio_env_names_are_not_read(monkeypatch):
    monkeypatch.delenv("STORAGE_ACCESS_KEY", raising=False)
    monkeypatch.setenv("MINIO_ACCESS_KEY", "old")
    with pytest.raises(Exception):
        settings(storage_secret_key="s")
