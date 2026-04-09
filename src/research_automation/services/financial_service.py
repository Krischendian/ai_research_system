"""
财务数据：优先 Financial Modeling Prep (FMP) 标准化接口；失败或无数据时回退 SEC EDGAR 10-K。
"""
from __future__ import annotations

import logging
from datetime import datetime, timezone

from research_automation.extractors import fmp_client
from research_automation.extractors.sec_edgar import get_financial_statements
from research_automation.models.financial import AnnualFinancials, CompanyFinancials

logger = logging.getLogger(__name__)


def _sec_company_search_url(symbol: str) -> str:
    """SEC EDGAR 按公司检索入口（便于用户手动核对 10-K）。"""
    s = (symbol or "").strip().upper()
    return (
        "https://www.sec.gov/cgi-bin/browse-edgar?"
        f"action=getcompany&owner=exclude&count=40&search_text={s}"
    )


def _fmp_source_url(symbol: str) -> str:
    s = (symbol or "").strip().upper()
    return (
        "https://financialmodelingprep.com/stable/income-statement"
        f"?symbol={s}&period=annual"
    )


def get_financials(ticker: str) -> CompanyFinancials:
    """
    拉取至多最近 3 个财年的 ``AnnualFinancials``（按财年降序，去重）。

    优先 ``fmp_client.get_financials``；若返回空或抛错则回退 ``sec_edgar.get_financial_statements``。
    """
    symbol = (ticker or "").strip().upper()
    now_iso = datetime.now(timezone.utc).isoformat()
    if not symbol:
        return CompanyFinancials(
            ticker="",
            financials=[],
            last_updated=now_iso,
            data_source=None,
            data_source_label="",
            primary_source_url=None,
        )

    fmp_rows: list[AnnualFinancials] = []
    try:
        fmp_rows = fmp_client.get_financials(symbol, years=3)
    except Exception:
        logger.exception("FMP 财务拉取异常 ticker=%s，将回退 SEC", symbol)

    if fmp_rows:
        return CompanyFinancials(
            ticker=symbol,
            financials=fmp_rows,
            last_updated=now_iso,
            data_source="FMP",
            data_source_label=(
                "数据来自 Financial Modeling Prep (FMP) API 年度报表与关键指标；"
                "货币与单位以 FMP 披露为准。"
            ),
            primary_source_url=_fmp_source_url(symbol),
        )

    now_y = datetime.now(timezone.utc).year
    sec_rows: list[AnnualFinancials] = []
    for filing_year in range(now_y, now_y - 5, -1):
        row = get_financial_statements(symbol, filing_year)
        if row is not None:
            sec_rows.append(row)

    by_fy: dict[int, AnnualFinancials] = {}
    for r in sec_rows:
        by_fy.setdefault(r.year, r)

    if not by_fy:
        logger.warning("SEC 财务解析无有效年度数据 ticker=%s", symbol)
        return CompanyFinancials(
            ticker=symbol,
            financials=[],
            last_updated=now_iso,
            data_source=None,
            data_source_label=(
                "暂无来自 FMP 或 SEC EDGAR 10-K Item 8 的可解析财务行；"
                "可配置 FMP_API_KEY 或确认 CIK/申报可读性。"
            ),
            primary_source_url=_sec_company_search_url(symbol),
        )

    ordered = sorted(by_fy.values(), key=lambda x: x.year, reverse=True)
    rows = ordered[:3]
    return CompanyFinancials(
        ticker=symbol,
        financials=rows,
        last_updated=now_iso,
        data_source="SEC",
        data_source_label=(
            "数据来自 SEC EDGAR 10-K 合并报表（Part II Item 8 表格解析）；"
            "以法定披露为准，解析字段可能不完整。"
        ),
        primary_source_url=_sec_company_search_url(symbol),
    )
