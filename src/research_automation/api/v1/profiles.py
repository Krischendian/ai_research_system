"""业务画像路由（v1，LLM + 示例节选）。"""
from typing import Annotated

from fastapi import APIRouter, HTTPException, Path

from research_automation.api.openapi_meta import COMMON_ERROR_RESPONSES
from research_automation.core.ticker_normalize import normalize_equity_ticker
from research_automation.models.company import BusinessProfile
from research_automation.services.profile_service import ProfileGenerationError, get_profile

router = APIRouter(prefix="/companies", tags=["profiles"])


@router.get(
    "/{ticker}/business-profile",
    response_model=BusinessProfile,
    summary="获取公司业务画像",
    responses={
        400: COMMON_ERROR_RESPONSES[400],
        500: COMMON_ERROR_RESPONSES[500],
        503: COMMON_ERROR_RESPONSES[503],
    },
)
def get_business_profile(
    ticker: Annotated[
        str,
        Path(description="股票代码", examples=["AAPL", "CTSH", "DHL GY"]),
    ],
) -> BusinessProfile:
    """基于 **SEC 10-K/20-F** 节选 + LLM 结构化抽取；质量不足时回退 FMP profile。"""
    try:
        return get_profile(normalize_equity_ticker(ticker))
    except ProfileGenerationError as e:
        raise HTTPException(status_code=503, detail=e.message) from e
    except Exception as e:
        raise HTTPException(
            status_code=500,
            detail=f"业务画像接口未预期错误：{type(e).__name__}：{e}",
        ) from e
