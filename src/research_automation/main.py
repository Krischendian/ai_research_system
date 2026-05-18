"""FastAPI 入口：路由、CORS、OpenAPI/Swagger。"""
from __future__ import annotations

import logging

from fastapi import FastAPI, HTTPException
from fastapi.middleware.cors import CORSMiddleware

from research_automation.api.openapi_meta import (
    API_DESCRIPTION,
    API_TITLE,
    API_VERSION,
    COMMON_ERROR_RESPONSES,
    OPENAPI_TAGS,
)
from research_automation.api.v1 import v1_router

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s [%(name)s] %(message)s",
)

app = FastAPI(
    title=API_TITLE,
    description=API_DESCRIPTION,
    version=API_VERSION,
    openapi_tags=OPENAPI_TAGS,
    docs_url="/docs",
    redoc_url="/redoc",
    openapi_url="/openapi.json",
)

app.include_router(
    v1_router,
    prefix="/api/v1",
    responses=COMMON_ERROR_RESPONSES,
)

app.add_middleware(
    CORSMiddleware,
    allow_origins=[
        "http://localhost:8501",
        "http://127.0.0.1:8501",
        "http://localhost:8502",
        "http://127.0.0.1:8502",
    ],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)


@app.get("/", tags=["health"], summary="服务根信息")
def root():
    """返回健康检查、Swagger 与主要 API 入口链接。"""
    return {
        "service": API_TITLE,
        "version": API_VERSION,
        "health": "/health",
        "docs": "/docs",
        "redoc": "/redoc",
        "openapi": "/openapi.json",
        "api_v1": "/api/v1",
    }


@app.get("/health", tags=["health"], summary="健康检查")
def health():
    """负载均衡 / 监控探活用。"""
    return {"status": "ok"}


@app.get("/hello", tags=["health"], summary="连通性烟测")
def hello():
    try:
        return {"message": "Hello from Research Automation API"}
    except Exception as e:
        raise HTTPException(
            status_code=500,
            detail=f"/hello 未预期错误：{type(e).__name__}：{e}",
        ) from e
