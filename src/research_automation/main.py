"""FastAPI 最小入口：仅健康检查。"""
from fastapi import FastAPI, HTTPException
from fastapi.middleware.cors import CORSMiddleware

from research_automation.api.v1 import v1_router

app = FastAPI(title="Research Automation API")

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
