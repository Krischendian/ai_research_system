"""财务数据路由（v1）。"""
import sqlite3
from datetime import datetime, timezone

from fastapi import APIRouter, HTTPException

from research_automation.core.database import read_financials
from research_automation.models.financial import CompanyFinancials

router = APIRouter(prefix="/companies", tags=["financials"])


@router.get("/{ticker}/financials", response_model=CompanyFinancials)
def get_company_financials(ticker: str) -> CompanyFinancials:
    """从 SQLite 读取该标的的年报财务数据，按 `CompanyFinancials` 返回。"""
    symbol = (ticker or "").strip().upper()
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
    yahoo = f"https://finance.yahoo.com/quote/{symbol}/"
    return CompanyFinancials(
        ticker=symbol,
        financials=rows_asc,
        last_updated=datetime.now(timezone.utc).isoformat(),
        data_source_label=(
            "本地 SQLite（`data/research.db`）← 由 yfinance 从 Yahoo Finance "
            "年度报表字段抓取入库；二级市场数据，请以公司法定披露（10-K/年报）为准。"
        ),
        primary_source_url=yahoo,
    )
