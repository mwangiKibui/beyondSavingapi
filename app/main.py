from fastapi import FastAPI

from app.core.config import get_settings
from app.core.redis import get_redis

app = FastAPI(title="beyondSaving API")


@app.get("/health")
async def health():
    settings = get_settings()

    try:
        await get_redis().ping()
        redis_status = "ok"
    except Exception:
        redis_status = "unreachable"

    return {"status": "ok", "env": settings.app_env, "redis": redis_status}
