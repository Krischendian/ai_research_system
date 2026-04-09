"""财务数据路由（v1）。"""
import sqlite3
from datetime import datetime, timezone

from fastapi import APIRouter, HTTPException

from research_automation.core.database import read_financials
from research_automation.core.ticker_normalize import normalize_equity_ticker
from research_automation.models.financial import CompanyFinancials

router = APIRouter(prefix="/companies", tags=["financials"])


@router.get("/{ticker}/financials", response_model=CompanyFinancials)
def get_company_financials(ticker: str) -> CompanyFinancials:
    """从 SQLite 读取该标的的年报财务数据，按 `CompanyFinancials` 返回。"""
    symbol = normalize_equity_ticker(ticker)
    if not symbol:
        raise HTTPException(
            status_code=400,
            detail="股票代码不能为空",
        )

    try:
        rows = read_financials(symbol)
    except sqlite3.Error as e:
        raise HTTPException(
            status_code=503,
            detail=f"数据库读取失败（SQLite）：{e}",
        ) from e
    except OSError as e:
        raise HTTPException(
            status_code=503,
            detail=f"无法访问数据库文件：{e}",
        ) from e
    except Exception as e:
        raise HTTPException(
            status_code=500,
            detail=f"读取财务数据时发生未预期错误：{type(e).__name__}: {e}",
        ) from e

    rows_asc = sorted(rows, key=lambda r: r.year)
    sec_url = (
        "https://www.sec.gov/cgi-bin/browse-edgar?"
        f"action=getcompany&owner=exclude&count=40&search_text={symbol}"
    )
    if rows_asc:
        return CompanyFinancials(
            ticker=symbol,
            financials=rows_asc,
            last_updated=datetime.now(timezone.utc).isoformat(),
            data_source="SEC EDGAR",
            data_source_label=(
                "本地 SQLite（`data/research.db`）← 由批量脚本自 **SEC EDGAR** "
                "10-K Item 8 解析入库；请以法定披露原文为准。"
            ),
            primary_source_url=sec_url,
        )
    return CompanyFinancials(
        ticker=symbol,
        financials=[],
        last_updated=datetime.now(timezone.utc).isoformat(),
        data_source=None,
        data_source_label=(
            "暂无入库的 SEC 财务行；可在项目根执行 "
            "`PYTHONPATH=src python scripts/batch_fetch_financials.py --ticker "
            f"{symbol} --force` 抓取后重试。"
        ),
        primary_source_url=sec_url,
    )
