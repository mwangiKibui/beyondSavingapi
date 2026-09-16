import logging
from contextlib import asynccontextmanager

import asyncpg
from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware

from app.api import accounts, auth, categories
from app.core.config import get_settings
from app.core.redis import get_redis
from app.core.storage import ensure_bucket, get_storage_client

# Root logger defaults to WARNING, which would silently swallow the app's
# own logger.info() calls (e.g. the password-reset dev-mode log) even
# though uvicorn's request logs still show up.
logging.basicConfig(level=logging.INFO)

logger = logging.getLogger(__name__)


@asynccontextmanager
async def lifespan(app: FastAPI):
    settings = get_settings()
    try:
        ensure_bucket(settings.minio_bucket)
    except Exception:
        logger.warning("Could not reach MinIO at startup; continuing in a degraded state.", exc_info=True)

    try:
        app.state.db_pool = await asyncpg.create_pool(settings.database_url, timeout=5)
    except Exception:
        logger.warning("Could not reach Postgres at startup; continuing in a degraded state.", exc_info=True)
        app.state.db_pool = None

    yield

    if app.state.db_pool is not None:
        await app.state.db_pool.close()


app = FastAPI(title="beyondSaving API", lifespan=lifespan)

_cors_origins = [origin.strip() for origin in get_settings().cors_origins.split(",")]
app.add_middleware(
    CORSMiddleware,
    allow_origins=_cors_origins,
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

app.include_router(auth.router)
app.include_router(accounts.router)
app.include_router(categories.router)


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

    db_pool = app.state.db_pool
    try:
        if db_pool is None:
            raise RuntimeError("no pool")
        await db_pool.fetchval("SELECT 1")
        db_status = "ok"
    except Exception:
        db_status = "unreachable"

    return {
        "status": "ok",
        "env": settings.app_env,
        "redis": redis_status,
        "storage": storage_status,
        "db": db_status,
    }
