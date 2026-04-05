"""FastAPI 启动入口。"""
from fastapi import FastAPI

from backend.app.api.v1 import financials, profiles

app = FastAPI(title="AI 投研分析系统 API", version="0.1.0")

app.include_router(financials.router, prefix="/api/v1")
app.include_router(profiles.router, prefix="/api/v1")


@app.get("/health")
def health():
    return {"status": "ok"}
