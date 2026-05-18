"""财报电话会分析路由（v1，FMP / earningscall 逐字稿 + LLM）。"""
from __future__ import annotations

import re

from typing import Annotated

from fastapi import APIRouter, HTTPException, Path, Query

from research_automation.api.openapi_meta import COMMON_ERROR_RESPONSES
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


@router.get(
    "/{ticker}/earnings",
    response_model=EarningsCallAnalysis,
    summary="获取财报电话会分析",
    responses={
        400: COMMON_ERROR_RESPONSES[400],
        500: COMMON_ERROR_RESPONSES[500],
        503: COMMON_ERROR_RESPONSES[503],
    },
)
def get_earnings_analysis(
    ticker: Annotated[str, Path(description="股票代码", examples=["AAPL", "CTSH", "DHL GY"])],
    quarter: Annotated[
        str,
        Query(
            description="财季，格式 `YYYYQN`（返回体中的 `quarter` 可能为 FMP 实际命中季度）",
            pattern=r"^\d{4}Q[1-4]$",
            examples=["2024Q4", "2026Q1"],
        ),
    ] = "2024Q4",
) -> EarningsCallAnalysis:
    """
    逐字稿来源：**FMP → EDGAR 8-K → sec-api.io**，再经 LLM 结构化总结。

    无逐字稿时返回 **503**。
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
