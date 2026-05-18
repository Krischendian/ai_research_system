"""自动化晨报（产品级 bundle API）。"""
from __future__ import annotations

from fastapi import APIRouter, HTTPException, Query

from research_automation.api.openapi_meta import COMMON_ERROR_RESPONSES
from research_automation.models.product import MorningBriefBundleResponse
from research_automation.services.morning_brief_bundle import get_morning_brief_bundle
from research_automation.services.news_service import NewsBriefError

router = APIRouter(prefix="/morning-brief", tags=["morning-brief"])


@router.get(
    "",
    response_model=MorningBriefBundleResponse,
    operation_id="get_morning_brief_bundle",
    summary="① 自动化晨报 — 拉取整包",
    description="返回 `overnight` + `yesterday_summary`；建议 `include_classic_brief=false` 以缩短耗时。",
    responses={500: COMMON_ERROR_RESPONSES[500], 503: COMMON_ERROR_RESPONSES[503]},
)
def morning_brief_bundle(
    sector: str | None = Query(
        None,
        description="监控板块，如 AI_Job_Replacement；传给隔夜/昨日过滤",
    ),
    include_classic_brief: bool = Query(
        True,
        description="是否同时拉取经典 RSS 宏观/公司晨报块",
    ),
) -> MorningBriefBundleResponse:
    try:
        return get_morning_brief_bundle(
            sector,
            include_classic_brief=include_classic_brief,
        )
    except NewsBriefError as e:
        raise HTTPException(status_code=503, detail=e.message) from e
    except Exception as e:
        raise HTTPException(
            status_code=500,
            detail=f"晨报 bundle 失败：{type(e).__name__}: {e}",
        ) from e
