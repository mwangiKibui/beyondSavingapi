from functools import lru_cache

import boto3
from botocore.config import Config

from app.core.config import Settings, get_settings


def build_storage_client(settings: Settings):
    return boto3.client(
        "s3",
        endpoint_url=settings.storage_endpoint,
        aws_access_key_id=settings.storage_access_key,
        aws_secret_access_key=settings.storage_secret_key,
        config=Config(signature_version="s3v4", s3={"addressing_style": "path"}),
        region_name=settings.storage_region,
    )


@lru_cache
def get_storage_client():
    return build_storage_client(get_settings())


def ensure_bucket(bucket: str) -> None:
    client = get_storage_client()
    existing = {b["Name"] for b in client.list_buckets().get("Buckets", [])}
    if bucket not in existing:
        client.create_bucket(Bucket=bucket)
