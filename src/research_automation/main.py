"""FastAPI 入口：路由、CORS、APScheduler 生命周期。"""
from __future__ import annotations

import logging
from contextlib import asynccontextmanager

from fastapi import FastAPI, HTTPException
from fastapi.middleware.cors import CORSMiddleware

from research_automation.api.v1 import v1_router
from research_automation.scheduler import shutdown_scheduler, start_scheduler

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s [%(name)s] %(message)s",
)


@asynccontextmanager
async def lifespan(app: FastAPI):
    start_scheduler()
    yield
    shutdown_scheduler()


app = FastAPI(title="Research Automation API", lifespan=lifespan)

app.include_router(v1_router, prefix="/api/v1")

app.add_middleware(
    CORSMiddleware,
    allow_origins=[
        "http://localhost:8501",
        "http://127.0.0.1:8501",
    ],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)


@app.get("/")
def root():
    return {
        "service": "Research Automation API",
        "health": "/health",
        "docs": "/docs",
        "system_status": "/api/v1/system/status",
    }


@app.get("/health")
def health():
    return {"status": "ok"}


@app.get("/hello")
def hello():
    try:
        return {"message": "Hello from backend"}
    except Exception as e:
        raise HTTPException(
            status_code=500,
            detail=f"/hello 未预期错误：{type(e).__name__}：{e}",
        ) from e
