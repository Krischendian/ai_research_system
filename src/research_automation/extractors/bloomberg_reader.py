"""Mac 端只读模块：从 DO PostgreSQL ``bloomberg`` schema 读取 ETL 采集的数据。

格式尽量对齐现有 FMP extractor 的返回结构（dict / list[dict]）。
"""
from __future__ import annotations

import logging
import os
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

import psycopg2
import psycopg2.extras
from dotenv import load_dotenv

logger = logging.getLogger(__name__)

_ENV_PATH = Path(__file__).resolve().parents[3] / ".env"
load_dotenv(_ENV_PATH, override=False)


def _get_conn() -> psycopg2.extensions.connection:
    return psycopg2.connect(
        host=os.getenv("DO_DB_HOST"),
        port=int(os.getenv("DO_DB_PORT", "25060")),
        dbname=os.getenv("DO_DB_NAME", "defaultdb"),
        user=os.getenv("DO_DB_USER"),
        password=os.getenv("DO_DB_PASSWORD"),
        sslmode=os.getenv("DO_DB_SSLMODE", "require"),
        cursor_factory=psycopg2.extras.RealDictCursor,
    )


def get_security_info(internal_ticker: str) -> dict[str, Any] | None:
    """标的基础信息：name, exchange, country, currency, sector。"""
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
                return [dict(r) for r in cur.fetchall()]
    except Exception as e:
        logger.warning("bloomberg_reader.get_financials_annual(%s): %s", internal_ticker, e)
        return []


def get_financials_quarterly(internal_ticker: str, quarters: int = 8) -> list[dict[str, Any]]:
    """季度财务：用于 Step6 图表等。"""
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
                return [dict(r) for r in cur.fetchall()]
    except Exception as e:
        logger.warning(
            "bloomberg_reader.get_financials_quarterly(%s): %s", internal_ticker, e
        )
        return []


def get_earnings_transcript(
    internal_ticker: str, fiscal_year: int, fiscal_quarter: int
) -> str | None:
    """电话会逐字稿（纯文本）。"""
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
                return str(row["transcript_text"]) if row and row.get("transcript_text") else None
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
