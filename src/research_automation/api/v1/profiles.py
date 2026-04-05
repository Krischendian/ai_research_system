"""业务画像路由（v1，LLM + 示例节选）。"""
from fastapi import APIRouter, HTTPException

from research_automation.models.company import BusinessProfile
from research_automation.services.profile_service import ProfileGenerationError, get_profile

router = APIRouter(prefix="/companies", tags=["profiles"])


@router.get("/{ticker}/business-profile", response_model=BusinessProfile)
def get_business_profile(ticker: str) -> BusinessProfile:
    """返回公司业务画像（基于示例文件节选 + LLM 抽取）。"""
    try:
        return get_profile(ticker)
    except ProfileGenerationError as e:
        raise HTTPException(status_code=503, detail=e.message) from e
    except Exception as e:
        raise HTTPException(
            status_code=500,
            detail=f"业务画像接口未预期错误：{type(e).__name__}：{e}",
        ) from e
