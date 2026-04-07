"""晨报新闻路由（v1：RSS + LLM）。"""
from fastapi import APIRouter, HTTPException

from research_automation.models.news import (
    MorningBrief,
    OvernightNewsResponse,
    YesterdaySummaryResponse,
)
from research_automation.services.daily_summary_service import get_yesterday_summary
from research_automation.services.news_service import NewsBriefError, get_morning_brief
from research_automation.services.overnight_service import get_overnight_news

router = APIRouter(prefix="/news", tags=["news"])


@router.get("/overnight", response_model=OvernightNewsResponse)
def overnight_brief() -> OvernightNewsResponse:
    """
    纽约时段「昨天 16:00～今天 08:00」内的 RSS 条目 + 一句中文隔夜要点（LLM）。
    """
    try:
        return get_overnight_news()
    except NewsBriefError as e:
        raise HTTPException(status_code=503, detail=e.message) from e
    except Exception as e:
        raise HTTPException(
            status_code=500,
            detail=f"隔夜速递接口未预期错误：{type(e).__name__}：{e}",
        ) from e


@router.get("/yesterday-summary", response_model=YesterdaySummaryResponse)
def yesterday_summary() -> YesterdaySummaryResponse:
    """
    纽约「昨日」全天（00:00–24:00）内有时间戳的 RSS 条目，经 LLM 分「宏观 / 公司」主题汇总。
    """
    try:
        return get_yesterday_summary()
    except NewsBriefError as e:
        raise HTTPException(status_code=503, detail=e.message) from e
    except Exception as e:
        raise HTTPException(
            status_code=500,
            detail=f"昨日总结接口未预期错误：{type(e).__name__}：{e}",
        ) from e


@router.get("/morning-brief", response_model=MorningBrief)
def morning_brief() -> MorningBrief:
    """
    拉取 Reuters/Bloomberg 等 RSS，经 LLM 生成中文摘要并分为宏观/公司。
    """
    try:
        return get_morning_brief()
    except NewsBriefError as e:
        raise HTTPException(status_code=503, detail=e.message) from e
    except Exception as e:
        raise HTTPException(
            status_code=500,
            detail=f"晨报接口未预期错误：{type(e).__name__}：{e}",
        ) from e
