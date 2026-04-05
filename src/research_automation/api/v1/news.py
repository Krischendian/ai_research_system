"""晨报新闻路由（v1：RSS + LLM）。"""
from fastapi import APIRouter, HTTPException

from research_automation.models.news import MorningBrief
from research_automation.services.news_service import NewsBriefError, get_morning_brief

router = APIRouter(prefix="/news", tags=["news"])


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
