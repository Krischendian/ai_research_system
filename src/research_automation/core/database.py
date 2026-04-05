"""SQLite：财务数据持久化（data/research.db）。"""
from __future__ import annotations

import sqlite3
import time
from pathlib import Path

from research_automation.models.financial import AnnualFinancials

_CACHE_TTL_SEC = 120.0
_financials_cache: dict[str, tuple[float, list[AnnualFinancials]]] = {}


def _project_root() -> Path:
    # database.py -> core -> research_automation -> src -> 项目根目录
    return Path(__file__).resolve().parents[3]


def get_db_path() -> Path:
    return _project_root() / "data" / "research.db"


def get_connection() -> sqlite3.Connection:
    path = get_db_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(path)
    conn.row_factory = sqlite3.Row
    return conn


def init_db(conn: sqlite3.Connection) -> None:
    """创建 financials 表，字段与 AnnualFinancials 对齐，并带 ticker。"""
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS financials (
            ticker TEXT NOT NULL,
            year INTEGER NOT NULL,
            revenue REAL,
            ebitda REAL,
            capex REAL,
            gross_margin REAL,
            net_debt_to_equity REAL,
            PRIMARY KEY (ticker, year)
        )
        """
    )
    conn.commit()


def invalidate_financials_cache(ticker: str | None = None) -> None:
    """写入后使缓存失效；``ticker`` 为 None 时清空全部。"""
    global _financials_cache
    if ticker is None:
        _financials_cache.clear()
        return
    sym = (ticker or "").strip().upper()
    _financials_cache.pop(sym, None)


def save_financials(ticker: str, data: list[AnnualFinancials]) -> None:
    """
    将年度财务数据写入数据库。
    同一 (ticker, year) 已存在则更新该行，避免重复插入。
    """
    symbol = (ticker or "").strip().upper()
    if not symbol or not data:
        return

    conn = get_connection()
    try:
        init_db(conn)
        for row in data:
            conn.execute(
                """
                INSERT INTO financials (
                    ticker, year, revenue, ebitda, capex, gross_margin, net_debt_to_equity
                ) VALUES (?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(ticker, year) DO UPDATE SET
                    revenue = excluded.revenue,
                    ebitda = excluded.ebitda,
                    capex = excluded.capex,
                    gross_margin = excluded.gross_margin,
                    net_debt_to_equity = excluded.net_debt_to_equity
                """,
                (
                    symbol,
                    row.year,
                    row.revenue,
                    row.ebitda,
                    row.capex,
                    row.gross_margin,
                    row.net_debt_to_equity,
                ),
            )
        conn.commit()
    finally:
        conn.close()
    invalidate_financials_cache(symbol)


def read_financials(ticker: str) -> list[AnnualFinancials]:
    """从数据库读取指定标的的财务年度数据（年份从新到旧）；带短期进程内缓存。"""
    symbol = (ticker or "").strip().upper()
    if not symbol:
        return []

    now = time.monotonic()
    hit = _financials_cache.get(symbol)
    if hit is not None:
        ts, rows = hit
        if now - ts < _CACHE_TTL_SEC:
            return [AnnualFinancials.model_validate(r.model_dump()) for r in rows]

    conn = get_connection()
    try:
        init_db(conn)
        cur = conn.execute(
            """
            SELECT year, revenue, ebitda, capex, gross_margin, net_debt_to_equity
            FROM financials
            WHERE ticker = ?
            ORDER BY year DESC
            """,
            (symbol,),
        )
        out: list[AnnualFinancials] = []
        for r in cur.fetchall():
            out.append(
                AnnualFinancials(
                    year=int(r["year"]),
                    revenue=r["revenue"],
                    ebitda=r["ebitda"],
                    capex=r["capex"],
                    gross_margin=r["gross_margin"],
                    net_debt_to_equity=r["net_debt_to_equity"],
                )
            )
        _financials_cache[symbol] = (now, out)
        return [AnnualFinancials.model_validate(r.model_dump()) for r in out]
    finally:
        conn.close()
