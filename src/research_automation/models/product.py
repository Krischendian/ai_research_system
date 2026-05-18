"""产品级 API 响应（行业报告、晨报 bundle）。"""
from __future__ import annotations

from typing import Any

from pydantic import BaseModel, Field

from research_automation.models.news import (
    MorningBrief,
    OvernightNewsResponse,
    YesterdaySummaryResponse,
)


class SectorReportCreateRequest(BaseModel):
    model_config = {
        "json_schema_extra": {
            "examples": [
                {
                    "sector": "AI_Job_Replacement",
                    "force_refresh": False,
                    "relevance_threshold": 1,
                }
            ]
        }
    }

    sector: str = Field(..., description="板块名称，如 AI_Job_Replacement")
    force_refresh: bool = Field(
        False,
        description="为 true 时跳过 SQLite 整份缓存并重新生成",
    )
    relevance_threshold: int | None = Field(
        None,
        ge=0,
        le=3,
        description="新闻 relevance 下限；默认读环境变量",
    )


class SectorReportResponse(BaseModel):
    sector: str
    year: int
    quarter: int
    report_md: str
    quarterly_data: dict[str, Any] = Field(default_factory=dict)
    from_cache: bool = False


class MorningBriefBundleResponse(BaseModel):
    """晨报页一次拉齐：隔夜 + 昨日总结 + 可选经典晨报结构。"""

    sector: str | None = None
    overnight: OvernightNewsResponse
    yesterday_summary: YesterdaySummaryResponse
    morning_brief: MorningBrief | None = Field(
        None,
        description="RSS 宏观/公司晨报；前端可暂不渲染",
    )
