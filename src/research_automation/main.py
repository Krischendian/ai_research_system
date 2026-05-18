"""FastAPI 入口：路由、CORS、OpenAPI/Swagger。"""
from __future__ import annotations

import logging

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware

from research_automation.api.openapi_meta import (
    API_DESCRIPTION,
    API_TITLE,
    API_VERSION,
    COMMON_ERROR_RESPONSES,
    OPENAPI_TAGS,
    PUBLIC_API_PATHS,
)
from research_automation.api.v1 import v1_router

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s [%(name)s] %(message)s",
)

def _custom_openapi():
    """Swagger 只包含 4 个产品 path（过滤运维路由）。"""
    if app.openapi_schema:
        return app.openapi_schema
    from fastapi.openapi.utils import get_openapi

    schema = get_openapi(
        title=API_TITLE,
        version=API_VERSION,
        description=API_DESCRIPTION,
        routes=app.routes,
    )
    schema["paths"] = {
        k: v for k, v in schema.get("paths", {}).items() if k in PUBLIC_API_PATHS
    }
    schema["tags"] = OPENAPI_TAGS
    app.openapi_schema = schema
    return app.openapi_schema


app = FastAPI(
    title=API_TITLE,
    description=API_DESCRIPTION,
    version=API_VERSION,
    openapi_tags=OPENAPI_TAGS,
    docs_url="/docs",
    redoc_url="/redoc",
    openapi_url="/openapi.json",
)

app.openapi = _custom_openapi

app.include_router(
    v1_router,
    prefix="/api/v1",
    responses=COMMON_ERROR_RESPONSES,
)

app.add_middleware(
    CORSMiddleware,
    allow_origins=[
        "http://localhost:3000",
        "http://127.0.0.1:3000",
        "http://localhost:5173",
        "http://127.0.0.1:5173",
        "http://localhost:8080",
        "http://127.0.0.1:8080",
    ],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)


@app.get("/", include_in_schema=False)
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


@app.get("/health", include_in_schema=False)
def health():
    """负载均衡 / 监控探活用。"""
    return {"status": "ok"}


