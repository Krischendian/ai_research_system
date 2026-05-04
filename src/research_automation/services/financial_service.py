"""
财务数据：优先 Financial Modeling Prep (FMP) 标准化接口；失败或无数据时回退 SEC EDGAR 10-K。
"""
from __future__ import annotations

import logging
from datetime import datetime, timezone

from research_automation.extractors import fmp_client
from research_automation.extractors.sec_edgar import get_financial_statements
from research_automation.extractors.bloomberg_reader import get_financials_annual as bbg_get_financials_annual
from research_automation.models.financial import AnnualFinancials, CompanyFinancials

logger = logging.getLogger(__name__)


def _bbg_to_float(v: object) -> float | None:
    if v is None:
        return None
    return float(v)


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

    # Bloomberg 优先：非美股标的从 DO PostgreSQL 读取 ETL 数据
    try:
        bbg_rows = bbg_get_financials_annual(symbol, years=3)
        if bbg_rows:
            financials: list[AnnualFinancials] = []
            for r in bbg_rows:
                rev = _bbg_to_float(r.get("revenue"))
                gp = _bbg_to_float(r.get("gross_profit"))
                gross_margin = (
                    (gp / rev) if rev and rev != 0 and gp is not None else None
                )
                debt = _bbg_to_float(r.get("total_debt"))
                cash = _bbg_to_float(r.get("cash"))
                equity = _bbg_to_float(r.get("total_equity"))
                net_debt_to_equity: float | None = None
                if equity is not None and equity != 0 and debt is not None:
                    c_adj = cash if cash is not None else 0.0
                    net_debt_to_equity = (debt - c_adj) / equity

                financials.append(
                    AnnualFinancials(
                        year=int(r["fiscal_year"]),
                        revenue=rev,
                        gross_margin=gross_margin,
                        ebitda=_bbg_to_float(r.get("ebitda")),
                        net_income=_bbg_to_float(r.get("net_income")),
                        capex=_bbg_to_float(r.get("capex")),
                        net_debt_to_equity=net_debt_to_equity,
                    )
                )
            logger.info("Bloomberg 财务数据命中 ticker=%s，%d 行", symbol, len(financials))
            return CompanyFinancials(
                ticker=symbol,
                financials=financials,
                last_updated=now_iso,
                data_source="Bloomberg",
                data_source_label=(
                    "数据来自 Bloomberg Terminal（BLPAPI）年度财务；"
                    "货币与单位以 Bloomberg 披露为准（百万本币）。"
                ),
                primary_source_url=f"https://www.bloomberg.com/quote/{symbol}:HK",
            )
    except Exception:
        logger.warning("Bloomberg 财务读取失败 ticker=%s，继续 FMP 链路", symbol, exc_info=True)

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
