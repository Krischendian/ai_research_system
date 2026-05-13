"""Mac 端只读模块：从 DO PostgreSQL ``bloomberg`` schema 读取 ETL 采集的数据。

格式尽量对齐现有 FMP extractor 的返回结构（dict / list[dict]）。
"""
from __future__ import annotations

import logging
import os
import re
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

from dotenv import load_dotenv

logger = logging.getLogger(__name__)

# Companies 种子简码 → 常见于 ETL ``internal_ticker`` 的 Bloomberg 风格代码
_BBG_INTERNAL_TICKER_FALLBACK: dict[str, str] = {
    "FRE": "FRE GY",
    "KBX": "KBX GY",
    "DHL": "DHL GY",
    "BT/A": "BT/A LN",
}

_ENV_PATH = Path(__file__).resolve().parents[3] / ".env"
load_dotenv(_ENV_PATH, override=False)


def _bbg_ready() -> bool:
    """未配置或显式关闭时不连库，避免不可达时每标的长时间 TCP 超时。"""
    flag = (os.getenv("BLOOMBERG_DB_ENABLED") or "1").strip().lower()
    if flag in ("0", "false", "no", "off"):
        return False
    host = (os.getenv("DO_DB_HOST") or "").strip()
    user = (os.getenv("DO_DB_USER") or "").strip()
    return bool(host and user)


def _get_conn() -> Any:
    import psycopg2  # lazy: optional Bloomberg / avoids import error at app startup
    import psycopg2.extras

    ct_raw = (os.getenv("DO_DB_CONNECT_TIMEOUT") or "8").strip()
    connect_timeout = int(ct_raw) if ct_raw.isdigit() else 8
    return psycopg2.connect(
        host=os.getenv("DO_DB_HOST"),
        port=int(os.getenv("DO_DB_PORT", "25060")),
        dbname=os.getenv("DO_DB_NAME", "defaultdb"),
        user=os.getenv("DO_DB_USER"),
        password=os.getenv("DO_DB_PASSWORD"),
        sslmode=os.getenv("DO_DB_SSLMODE", "require"),
        connect_timeout=connect_timeout,
        cursor_factory=psycopg2.extras.RealDictCursor,
    )


def get_security_info(internal_ticker: str) -> dict[str, Any] | None:
    """标的基础信息：name, exchange, country, currency, sector。"""
    if not _bbg_ready():
        return None
    try:
        with _get_conn() as conn:
            with conn.cursor() as cur:
                cur.execute(
                    """
                    SELECT bloomberg_ticker, name, exchange_code,
                           country_iso, currency, gics_sector, gics_industry, updated_at
                    FROM bloomberg.securities
                    WHERE internal_ticker = %s
                    """,
                    (internal_ticker,),
                )
                row = cur.fetchone()
                return dict(row) if row else None
    except Exception as e:
        logger.warning("bloomberg_reader.get_security_info(%s): %s", internal_ticker, e)
        return None


def get_financials_annual(internal_ticker: str, years: int = 5) -> list[dict[str, Any]]:
    """年度财务：按 ``fiscal_year`` 降序，格式对齐现有 FMP 结构。"""
    if not _bbg_ready():
        return []
    try:
        with _get_conn() as conn:
            with conn.cursor() as cur:
                cur.execute(
                    """
                    SELECT f.fiscal_year, f.revenue, f.gross_profit, f.ebitda,
                           f.net_income, f.capex, f.total_debt, f.cash,
                           f.total_equity, f.fetched_at
                    FROM bloomberg.financials_annual f
                    JOIN bloomberg.securities s USING (bloomberg_ticker)
                    WHERE s.internal_ticker = %s
                    ORDER BY f.fiscal_year DESC
                    LIMIT %s
                    """,
                    (internal_ticker, years),
                )
                rows = [dict(r) for r in cur.fetchall()]
                if rows:
                    return rows
                fb = _BBG_INTERNAL_TICKER_FALLBACK.get(
                    str(internal_ticker or "").strip().upper()
                )
                if fb and fb != internal_ticker:
                    cur.execute(
                        """
                        SELECT f.fiscal_year, f.revenue, f.gross_profit, f.ebitda,
                               f.net_income, f.capex, f.total_debt, f.cash,
                               f.total_equity, f.fetched_at
                        FROM bloomberg.financials_annual f
                        JOIN bloomberg.securities s USING (bloomberg_ticker)
                        WHERE s.internal_ticker = %s
                        ORDER BY f.fiscal_year DESC
                        LIMIT %s
                        """,
                        (fb, years),
                    )
                    return [dict(r) for r in cur.fetchall()]
                return rows
    except Exception as e:
        logger.warning("bloomberg_reader.get_financials_annual(%s): %s", internal_ticker, e)
        return []


def get_financials_quarterly(internal_ticker: str, quarters: int = 8) -> list[dict[str, Any]]:
    """季度财务：用于 Step6 图表等。"""
    if not _bbg_ready():
        return []
    try:
        with _get_conn() as conn:
            with conn.cursor() as cur:
                cur.execute(
                    """
                    SELECT f.fiscal_year, f.fiscal_quarter, f.period_end_date,
                           f.revenue, f.gross_profit, f.ebitda, f.capex, f.fetched_at
                    FROM bloomberg.financials_quarterly f
                    JOIN bloomberg.securities s USING (bloomberg_ticker)
                    WHERE s.internal_ticker = %s
                    ORDER BY f.fiscal_year DESC, f.fiscal_quarter DESC
                    LIMIT %s
                    """,
                    (internal_ticker, quarters),
                )
                rows = [dict(r) for r in cur.fetchall()]
                if rows:
                    return rows
                fb = _BBG_INTERNAL_TICKER_FALLBACK.get(
                    str(internal_ticker or "").strip().upper()
                )
                if fb and fb != internal_ticker:
                    cur.execute(
                        """
                        SELECT f.fiscal_year, f.fiscal_quarter, f.period_end_date,
                               f.revenue, f.gross_profit, f.ebitda, f.capex, f.fetched_at
                        FROM bloomberg.financials_quarterly f
                        JOIN bloomberg.securities s USING (bloomberg_ticker)
                        WHERE s.internal_ticker = %s
                        ORDER BY f.fiscal_year DESC, f.fiscal_quarter DESC
                        LIMIT %s
                        """,
                        (fb, quarters),
                    )
                    return [dict(r) for r in cur.fetchall()]
                return rows
    except Exception as e:
        logger.warning(
            "bloomberg_reader.get_financials_quarterly(%s): %s", internal_ticker, e
        )
        return []


def get_earnings_transcript(
    internal_ticker: str, fiscal_year: int, fiscal_quarter: int
) -> str | None:
    """电话会逐字稿（纯文本）。"""
    if not _bbg_ready():
        return None
    try:
        with _get_conn() as conn:
            with conn.cursor() as cur:
                cur.execute(
                    """
                    SELECT t.transcript_text
                    FROM bloomberg.earnings_transcripts t
                    JOIN bloomberg.securities s USING (bloomberg_ticker)
                    WHERE s.internal_ticker = %s
                      AND t.fiscal_year = %s AND t.fiscal_quarter = %s
                    """,
                    (internal_ticker, fiscal_year, fiscal_quarter),
                )
                row = cur.fetchone()
                txt = str(row["transcript_text"]) if row and row.get("transcript_text") else None
                if txt:
                    return txt
                fb = _BBG_INTERNAL_TICKER_FALLBACK.get(
                    str(internal_ticker or "").strip().upper()
                )
                if fb and fb != internal_ticker:
                    cur.execute(
                        """
                        SELECT t.transcript_text
                        FROM bloomberg.earnings_transcripts t
                        JOIN bloomberg.securities s USING (bloomberg_ticker)
                        WHERE s.internal_ticker = %s
                          AND t.fiscal_year = %s AND t.fiscal_quarter = %s
                        """,
                        (fb, fiscal_year, fiscal_quarter),
                    )
                    row2 = cur.fetchone()
                    return (
                        str(row2["transcript_text"])
                        if row2 and row2.get("transcript_text")
                        else None
                    )
                return None
    except Exception as e:
        logger.warning(
            "bloomberg_reader.get_earnings_transcript(%s, %sQ%s): %s",
            internal_ticker,
            fiscal_year,
            fiscal_quarter,
            e,
        )
        return None


def is_data_fresh(internal_ticker: str, max_age_days: int = 7) -> bool:
    """检查年度财务数据是否在有效期内（按 ``fetched_at``）。"""
    if not _bbg_ready():
        return False
    try:
        with _get_conn() as conn:
            with conn.cursor() as cur:
                cur.execute(
                    """
                    SELECT MAX(f.fetched_at) AS last_fetch
                    FROM bloomberg.financials_annual f
                    JOIN bloomberg.securities s USING (bloomberg_ticker)
                    WHERE s.internal_ticker = %s
                    """,
                    (internal_ticker,),
                )
                row = cur.fetchone()
                if not row or not row.get("last_fetch"):
                    return False
                last: datetime = row["last_fetch"]
                if last.tzinfo is None:
                    last = last.replace(tzinfo=timezone.utc)
                age = datetime.now(timezone.utc) - last
                return age <= timedelta(days=max_age_days)
    except Exception as e:
        logger.warning("bloomberg_reader.is_data_fresh(%s): %s", internal_ticker, e)
        return False

# ── 以下函数添加到 bloomberg_reader.py 末尾 ──────────────────


def _bloomberg_equity_display_to_internal(ticker: str) -> str:
    """``IBM US Equity`` → ``IBM``；无 ``… XX Equity`` 后缀则原样返回（如 ``FRE GY``）。"""
    s = (ticker or "").strip()
    if not s:
        return s
    m = re.match(r"^(.+?)\s+[A-Z]{1,3}\s+[Ee]quity$", s)
    if m:
        return m.group(1).strip()
    return s


def get_geo_revenue(
    internal_ticker: str,
    years: int = 5,
) -> list[dict[str, Any]]:
    """地理收入分布，从 bloomberg.geo_revenue 读取。
 
    返回格式（按 period_label DESC, geography_name ASC 排序）::
 
        [
            {"period_label": "FY 2025", "geography_name": "Americas",
             "revenue": 33342.0, "source_name": "Bloomberg PG_REVENUE"},
            {"period_label": "FY 2025", "geography_name": "Asia Pacific",
             "revenue": 12004.0, "source_name": "Bloomberg PG_REVENUE"},
            ...
        ]
 
    ``period_label`` 格式为 Bloomberg 原始值，如 ``"FY 2025"``。
    调用方可用 ``period_label.split()[-1]`` 取年份整数。

    支持传入展示用代码（如 ``IBM US Equity``），会先规范为库表中的 ``internal_ticker`` 再查询。
    """
    if not _bbg_ready():
        return []
    raw = str(internal_ticker or "").strip()
    if not raw:
        return []
    base = _bloomberg_equity_display_to_internal(raw).upper()
    raw_u = raw.upper()
    try:
        with _get_conn() as conn:
            with conn.cursor() as cur:
                keys: list[str] = []
                if base:
                    keys.append(base)
                if raw_u and raw_u not in keys:
                    keys.append(raw_u)
                for key in keys:
                    rows = _fetch_geo(cur, key, years)
                    if rows:
                        return rows
                # fallback：非美股简码映射（按规范后简码查）
                fb = _BBG_INTERNAL_TICKER_FALLBACK.get(base) or _BBG_INTERNAL_TICKER_FALLBACK.get(
                    raw_u
                )
                if fb and fb.upper() not in {k.upper() for k in keys}:
                    rows = _fetch_geo(cur, fb, years)
                    if rows:
                        return rows
                return []
    except Exception as e:
        logger.warning(
            "bloomberg_reader.get_geo_revenue(%s): %s", internal_ticker, e
        )
        return []
 
 
def _fetch_geo(cur: Any, ticker: str, years: int) -> list[dict[str, Any]]:
    """内部辅助：按 internal_ticker 或 fallback ticker 查 geo_revenue。"""
    # 取最近 N 个不同 period_label（年份），再拿这些年的所有地区
    cur.execute(
        """
        WITH latest_periods AS (
            SELECT DISTINCT g.period_label
            FROM bloomberg.geo_revenue g
            JOIN bloomberg.securities s USING (bloomberg_ticker)
            WHERE s.internal_ticker = %s
            ORDER BY g.period_label DESC
            LIMIT %s
        )
        SELECT g.period_label,
               g.geography_name,
               g.revenue,
               g.source_name
        FROM bloomberg.geo_revenue g
        JOIN bloomberg.securities s USING (bloomberg_ticker)
        WHERE s.internal_ticker = %s
          AND g.period_label IN (SELECT period_label FROM latest_periods)
        ORDER BY g.period_label DESC, g.geography_name ASC
        """,
        (ticker, years, ticker),
    )
    return [dict(r) for r in cur.fetchall()]


def get_segment_revenue(
    internal_ticker: str,
    years: int = 5,
) -> list[dict[str, Any]]:
    """业务分部收入，从 bloomberg.segment_revenue 读取。

    返回格式（按 fiscal_year DESC, revenue DESC 排序）::

        [
            {"fiscal_year": 2025, "segment_name": "Express",
             "revenue": 23805.0},
            {"fiscal_year": 2025, "segment_name": "Supply Chain",
             "revenue": 17689.0},
            ...
        ]

    revenue 单位为 Millions（与 Bloomberg 原始单位一致）。
    """
    if not _bbg_ready():
        return []
    raw = str(internal_ticker or "").strip()
    if not raw:
        return []
    base = _bloomberg_equity_display_to_internal(raw).upper()
    raw_u = raw.upper()
    try:
        with _get_conn() as conn:
            with conn.cursor() as cur:
                keys: list[str] = []
                if base:
                    keys.append(base)
                if raw_u and raw_u not in keys:
                    keys.append(raw_u)
                for key in keys:
                    rows = _fetch_segment(cur, key, years)
                    if rows:
                        return rows
                fb = _BBG_INTERNAL_TICKER_FALLBACK.get(base) or _BBG_INTERNAL_TICKER_FALLBACK.get(
                    raw_u
                )
                if fb and fb.upper() not in {k.upper() for k in keys}:
                    rows = _fetch_segment(cur, fb, years)
                    if rows:
                        return rows
                return []
    except Exception as e:
        logger.warning(
            "bloomberg_reader.get_segment_revenue(%s): %s", internal_ticker, e
        )
        return []


def _fetch_segment(cur: Any, ticker: str, years: int) -> list[dict[str, Any]]:
    """内部辅助：按 internal_ticker 或 fallback ticker 查 segment_revenue。"""
    cur.execute(
        """
        WITH latest_years AS (
            SELECT DISTINCT sr.fiscal_year
            FROM bloomberg.segment_revenue sr
            JOIN bloomberg.securities s USING (bloomberg_ticker)
            WHERE s.internal_ticker = %s
            ORDER BY sr.fiscal_year DESC
            LIMIT %s
        )
        SELECT sr.fiscal_year,
               sr.segment_name,
               sr.revenue
        FROM bloomberg.segment_revenue sr
        JOIN bloomberg.securities s USING (bloomberg_ticker)
        WHERE s.internal_ticker = %s
          AND sr.fiscal_year IN (SELECT fiscal_year FROM latest_years)
        ORDER BY sr.fiscal_year DESC, sr.revenue DESC
        """,
        (ticker, years, ticker),
    )
    return [dict(r) for r in cur.fetchall()]


def get_insider_monthly(
    internal_ticker: str,
    months: int = 12,
) -> list[dict[str, Any]]:
    """内部人月度交易汇总，从 bloomberg.insider_monthly 读取。

    返回格式（按 month DESC 排序，只返回有实际交易的月份）::

        [
            {"month": "02/2026", "net_transactions": 2,
             "shares_bought": 66432.0, "shares_sold": -33776.0,
             "net_shares": 32656.0, "close_price": 240.21,
             "volume": 143528000.0},
            ...
        ]

    month 格式为 ``"MM/YYYY"``。
    shares_sold 为负值（Bloomberg 原始值）。
    """
    if not _bbg_ready():
        return []
    raw = str(internal_ticker or "").strip()
    if not raw:
        return []
    base = _bloomberg_equity_display_to_internal(raw).upper()
    raw_u = raw.upper()
    try:
        with _get_conn() as conn:
            with conn.cursor() as cur:
                keys: list[str] = []
                if base:
                    keys.append(base)
                if raw_u and raw_u not in keys:
                    keys.append(raw_u)
                for key in keys:
                    rows = _fetch_insider(cur, key, months)
                    if rows:
                        return rows
                fb = _BBG_INTERNAL_TICKER_FALLBACK.get(base) or _BBG_INTERNAL_TICKER_FALLBACK.get(
                    raw_u
                )
                if fb and fb.upper() not in {k.upper() for k in keys}:
                    rows = _fetch_insider(cur, fb, months)
                    if rows:
                        return rows
                return []
    except Exception as e:
        logger.warning(
            "bloomberg_reader.get_insider_monthly(%s): %s", internal_ticker, e
        )
        return []


def _fetch_insider(cur: Any, ticker: str, months: int) -> list[dict[str, Any]]:
    """内部辅助：按 internal_ticker 或 fallback ticker 查 insider_monthly。"""
    cur.execute(
        """
        SELECT im.month,
               im.net_transactions,
               CASE WHEN ABS(im.shares_bought) < 1e-10 THEN NULL
                    ELSE im.shares_bought END AS shares_bought,
               CASE WHEN ABS(im.shares_sold) < 1e-10 THEN NULL
                    ELSE im.shares_sold END AS shares_sold,
               CASE WHEN ABS(im.net_shares) < 1e-10 THEN NULL
                    ELSE im.net_shares END AS net_shares,
               im.close_price,
               im.volume
        FROM bloomberg.insider_monthly im
        JOIN bloomberg.securities s USING (bloomberg_ticker)
        WHERE s.internal_ticker = %s
        ORDER BY im.month DESC
        LIMIT %s
        """,
        (ticker, months),
    )
    return [dict(r) for r in cur.fetchall()]