from fastapi import FastAPI

from app.core.config import get_settings

app = FastAPI(title="beyondSaving API")


@app.get("/health")
def health():
    settings = get_settings()
    return {"status": "ok", "env": settings.app_env}
