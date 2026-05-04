"""财务数据路由（v1）。"""

from fastapi import APIRouter, HTTPException

from research_automation.core.ticker_normalize import normalize_equity_ticker
from research_automation.models.financial import CompanyFinancials
from research_automation.services.financial_service import get_financials

router = APIRouter(prefix="/companies", tags=["financials"])


@router.get("/{ticker}/financials", response_model=CompanyFinancials)
def get_company_financials(ticker: str) -> CompanyFinancials:
    """优先 Bloomberg → FMP → SEC EDGAR 三级回退，返回年报财务数据。"""
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
