"""财报电话会分析路由（v1，FMP / earningscall 逐字稿 + LLM）。"""
from __future__ import annotations

import re

from fastapi import APIRouter, HTTPException

from research_automation.core.ticker_normalize import normalize_equity_ticker
from research_automation.models.earnings import EarningsCallAnalysis
from research_automation.services.earnings_service import EarningsAnalysisError, analyze_earnings_call

router = APIRouter(prefix="/companies", tags=["earnings"])

_QUARTER_RE = re.compile(r"^(\d{4})\s*Q\s*([1-4])\s*$", re.IGNORECASE)


def _parse_quarter(q: str) -> tuple[int, int]:
    m = _QUARTER_RE.match((q or "").strip())
    if not m:
        raise HTTPException(
            status_code=400,
            detail="query 参数 quarter 格式须为 YYYYQN，例如 2024Q4",
        )
    return int(m.group(1)), int(m.group(2))


@router.get("/{ticker}/earnings", response_model=EarningsCallAnalysis)
def get_earnings_analysis(
    ticker: str,
    quarter: str = "2024Q4",
) -> EarningsCallAnalysis:
    """
    返回指定季度财报电话会分析（FMP → EDGAR 8-K → earningscall → sec-api.io + OpenAI 结构化总结）。

    无逐字稿时 HTTP 503。示例：``GET /api/v1/companies/AAPL/earnings?quarter=2024Q4``
    """
    symbol = normalize_equity_ticker(ticker)
    if not symbol:
        raise HTTPException(status_code=400, detail="股票代码不能为空")
    year, qn = _parse_quarter(quarter)
    try:
        return analyze_earnings_call(symbol, year, qn)
    except EarningsAnalysisError as e:
        raise HTTPException(status_code=503, detail=e.message) from e
    except Exception as e:
        raise HTTPException(
            status_code=500,
            detail=f"电话会接口未预期错误：{type(e).__name__}：{e}",
        ) from e
