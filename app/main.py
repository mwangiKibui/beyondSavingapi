from fastapi import FastAPI

app = FastAPI(title="beyondSaving API")


@app.get("/health")
def health():
    return {"status": "ok"}
