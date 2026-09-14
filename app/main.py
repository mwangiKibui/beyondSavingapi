from contextlib import asynccontextmanager

from fastapi import FastAPI

from app.core.config import get_settings
from app.core.redis import get_redis
from app.core.storage import ensure_bucket, get_storage_client


@asynccontextmanager
async def lifespan(app: FastAPI):
    settings = get_settings()
    ensure_bucket(settings.minio_bucket)
    yield


app = FastAPI(title="beyondSaving API", lifespan=lifespan)


@app.get("/health")
async def health():
    settings = get_settings()

    try:
        await get_redis().ping()
        redis_status = "ok"
    except Exception:
        redis_status = "unreachable"

    try:
        get_storage_client().head_bucket(Bucket=settings.minio_bucket)
        storage_status = "ok"
    except Exception:
        storage_status = "unreachable"

    return {
        "status": "ok",
        "env": settings.app_env,
        "redis": redis_status,
        "storage": storage_status,
    }
