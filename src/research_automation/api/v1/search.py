"""全局关键词搜索 API。"""
from __future__ import annotations

from typing import Any

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel, Field

from research_automation.services.search_service import answer_question, keyword_search

router = APIRouter(tags=["search"])


class SearchRequest(BaseModel):
    query: str = Field("", description="搜索关键词（空白则返回空列表）")
    limit: int = Field(20, ge=1, le=200, description="最大返回条数")


class SearchResponse(BaseModel):
    results: list[dict[str, Any]]


@router.post("/search", response_model=SearchResponse)
def post_search(body: SearchRequest) -> SearchResponse:
    rows = keyword_search(body.query, limit=body.limit)
    return SearchResponse(results=rows)


class AskRequest(BaseModel):
    question: str = Field("", description="用户问题")


class AskResponse(BaseModel):
    answer: str
    sources: list[dict[str, Any]]


@router.post("/search/ask", response_model=AskResponse)
def post_search_ask(body: AskRequest) -> AskResponse:
    """基于关键词检索片段的 RAG 简答（gpt-4o-mini）。"""
    try:
        out = answer_question(body.question)
    except ValueError as e:
        raise HTTPException(
            status_code=503,
            detail=f"语言模型未就绪：{e}",
        ) from e
    except RuntimeError as e:
        raise HTTPException(
            status_code=503,
            detail=f"调用语言模型失败：{e}",
        ) from e
    except Exception as e:
        raise HTTPException(
            status_code=500,
            detail=f"问答接口未预期错误：{type(e).__name__}：{e}",
        ) from e
    return AskResponse(
        answer=str(out.get("answer") or ""),
        sources=out.get("sources") if isinstance(out.get("sources"), list) else [],
    )
