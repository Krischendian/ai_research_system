"""
季度报告缓存层：把生成好的sector报告存入SQLite，避免重复调用LLM。
缓存key = sector + year + quarter
"""
from __future__ import annotations

import logging
from datetime import datetime, timezone

from research_automation.core.database import get_connection, init_db

logger = logging.getLogger(__name__)

_CACHE_TABLE_SQL = """
CREATE TABLE IF NOT EXISTS sector_report_cache (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    sector TEXT NOT NULL,
    year INTEGER NOT NULL,
    quarter INTEGER NOT NULL,
    report_md TEXT NOT NULL,
    generated_at TEXT NOT NULL,
    created_at TEXT NOT NULL DEFAULT (datetime('now'))
);
CREATE UNIQUE INDEX IF NOT EXISTS uix_sector_report_cache
    ON sector_report_cache (sector, year, quarter);
"""


def _ensure_table(conn) -> None:
    for stmt in _CACHE_TABLE_SQL.strip().split(";"):
        s = stmt.strip()
        if s:
            conn.execute(s)
    conn.commit()


def get_cached_report(sector: str, year: int, quarter: int) -> str | None:
    """
    读取缓存报告。有缓存返回markdown字符串，无缓存返回None。
    """
    conn = get_connection()
    try:
        init_db(conn)
        _ensure_table(conn)
        cur = conn.execute(
            "SELECT report_md, generated_at FROM sector_report_cache "
            "WHERE sector=? AND year=? AND quarter=?",
            (sector, year, quarter),
        )
        row = cur.fetchone()
        if row:
            logger.info("命中缓存 sector=%s %dQ%d generated_at=%s", sector, year, quarter, row[1])
            return str(row[0])
        return None
    finally:
        conn.close()


def save_report_cache(sector: str, year: int, quarter: int, report_md: str) -> None:
    """
    保存报告到缓存。同一sector+year+quarter覆盖旧缓存。
    """
    now = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC")
    conn = get_connection()
    try:
        init_db(conn)
        _ensure_table(conn)
        conn.execute(
            """
            INSERT INTO sector_report_cache (sector, year, quarter, report_md, generated_at)
            VALUES (?, ?, ?, ?, ?)
            ON CONFLICT(sector, year, quarter) DO UPDATE SET
                report_md=excluded.report_md,
                generated_at=excluded.generated_at
            """,
            (sector, year, quarter, report_md, now),
        )
        conn.commit()
        logger.info("报告已缓存 sector=%s %dQ%d", sector, year, quarter)
    finally:
        conn.close()


def delete_report_cache(sector: str, year: int, quarter: int) -> None:
    """强制删除缓存，用于前端'强制刷新'按钮。"""
    conn = get_connection()
    try:
        init_db(conn)
        _ensure_table(conn)
        conn.execute(
            "DELETE FROM sector_report_cache WHERE sector=? AND year=? AND quarter=?",
            (sector, year, quarter),
        )
        conn.commit()
        logger.info("缓存已删除 sector=%s %dQ%d", sector, year, quarter)
    finally:
        conn.close()


def list_cached_reports() -> list[dict]:
    """列出所有已缓存的报告（供前端显示）。"""
    conn = get_connection()
    try:
        init_db(conn)
        _ensure_table(conn)
        cur = conn.execute(
            "SELECT sector, year, quarter, generated_at FROM sector_report_cache ORDER BY generated_at DESC"
        )
        return [
            {"sector": r[0], "year": r[1], "quarter": r[2], "generated_at": r[3]}
            for r in cur.fetchall()
        ]
    finally:
        conn.close()
