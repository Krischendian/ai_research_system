"""从 Yahoo Finance（yfinance）抓取年度财务数据。"""
from __future__ import annotations

import logging
from typing import Any

import pandas as pd
import yfinance as yf

from research_automation.models.financial import AnnualFinancials

logger = logging.getLogger(__name__)


def _to_float(val: Any) -> float | None:
    """将单元格标量转为 float；无法转换或缺值为 None。"""
    if val is None:
        return None
    try:
        if isinstance(val, float) and pd.isna(val):
            return None
        if isinstance(val, (int, float)):
            return float(val)
        if hasattr(val, "item"):
            v = val.item()
            if isinstance(v, float) and pd.isna(v):
                return None
            return float(v)
    except (TypeError, ValueError):
        return None
    return None


def _get_row_value(df: pd.DataFrame | None, col: Any, row_names: list[str]) -> float | None:
    """按备选行名顺次读取某一列，取第一个存在的行。"""
    if df is None or df.empty or col not in df.columns:
        return None
    for name in row_names:
        if name in df.index:
            return _to_float(df.loc[name, col])
    return None


def _fiscal_year(col: Any) -> int | None:
    """从报表列时间戳推断财政年度（取日历年）。"""
    try:
        return int(pd.Timestamp(col).year)
    except Exception:
        return None


def get_financials(ticker: str) -> list[AnnualFinancials]:
    """
    抓取最近 3 个财年（以利润表可用年度为准）的主要指标。

    指标：Revenue、EBITDA、Gross Margin、CAPEX、Net Debt/Equity。
    某一字段若 yfinance 无数据或解析失败，对应属性为 None，整体不抛异常。
    """
    out: list[AnnualFinancials] = []
    symbol = (ticker or "").strip().upper()
    if not symbol:
        return out

    try:
        stock = yf.Ticker(symbol)
        inc = stock.income_stmt
        bs = stock.balance_sheet
        cf = stock.cashflow
    except Exception as exc:
        logger.warning("yfinance 拉取报表失败 ticker=%s: %s", symbol, exc)
        return out

    try:
        if inc is None or inc.empty:
            return out
        cols = list(sorted(inc.columns, reverse=True)[:3])
    except Exception as exc:
        logger.warning("解析利润表列失败 ticker=%s: %s", symbol, exc)
        return out

    for col in cols:
        try:
            yr = _fiscal_year(col)
            if yr is None:
                continue

            revenue = _get_row_value(inc, col, ["Total Revenue", "Operating Revenue"])
            ebitda = _get_row_value(inc, col, ["EBITDA", "Normalized EBITDA"])

            gross_profit = _get_row_value(inc, col, ["Gross Profit"])
            gross_margin: float | None = None
            if revenue not in (None, 0.0) and gross_profit is not None:
                gross_margin = gross_profit / revenue

            capex_raw = _get_row_value(cf, col, ["Capital Expenditure"])
            capex = abs(capex_raw) if capex_raw is not None else None

            net_debt = _get_row_value(bs, col, ["Net Debt"])
            equity = _get_row_value(
                bs,
                col,
                [
                    "Stockholders Equity",
                    "Common Stock Equity",
                    "Total Equity Gross Minority Interest",
                ],
            )
            net_debt_to_equity: float | None = None
            if net_debt is not None and equity not in (None, 0.0):
                net_debt_to_equity = net_debt / equity

            out.append(
                AnnualFinancials(
                    year=yr,
                    revenue=revenue,
                    ebitda=ebitda,
                    capex=capex,
                    gross_margin=gross_margin,
                    net_debt_to_equity=net_debt_to_equity,
                )
            )
        except Exception as exc:
            logger.warning("解析单财年数据失败 ticker=%s period=%s: %s", symbol, col, exc)
            continue

    return out
