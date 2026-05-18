"""财务数据路由（v1）。"""

from typing import Annotated

from fastapi import APIRouter, HTTPException, Path

from research_automation.api.openapi_meta import COMMON_ERROR_RESPONSES
from research_automation.core.ticker_normalize import normalize_equity_ticker
from research_automation.models.financial import CompanyFinancials
from research_automation.services.financial_service import get_financials

router = APIRouter(prefix="/companies", tags=["financials"])


@router.get(
    "/{ticker}/financials",
    response_model=CompanyFinancials,
    summary="获取公司年报财务",
    responses={400: COMMON_ERROR_RESPONSES[400], 500: COMMON_ERROR_RESPONSES[500]},
)
def get_company_financials(
    ticker: Annotated[
        str,
        Path(
            description="股票代码（支持 `AAPL`、`ACN`、`IBM US Equity` 等，内部会规范化）",
            examples=["AAPL", "ACN", "IBM US Equity"],
        ),
    ],
) -> CompanyFinancials:
    """优先 **Bloomberg → FMP → SEC EDGAR** 回退，返回最近若干财年的年报指标。"""
    symbol = normalize_equity_ticker(ticker)
    if not symbol:
        raise HTTPException(status_code=400, detail="股票代码不能为空")
    try:
        return get_financials(symbol)
    except Exception as e:
        raise HTTPException(
            status_code=500,
            detail=f"读取财务数据时发生未预期错误：{type(e).__name__}: {e}",
        ) from e
