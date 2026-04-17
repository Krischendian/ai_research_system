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


def _ensure_companies_columns(conn: sqlite3.Connection) -> None:
    """为既有库补齐 ``companies`` 列（如 ``bloomberg_ticker``）。"""
    cur = conn.execute("PRAGMA table_info(companies)")
    cols = {str(r[1]) for r in cur.fetchall()}
    if "bloomberg_ticker" not in cols:
        conn.execute("ALTER TABLE companies ADD COLUMN bloomberg_ticker TEXT")


def init_db(conn: sqlite3.Connection) -> None:
    """创建 financials、companies、document_paragraphs 等表。"""
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
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS companies (
            ticker TEXT NOT NULL PRIMARY KEY,
            company_name TEXT NOT NULL,
            sector TEXT NOT NULL,
            country TEXT NOT NULL,
            is_active INTEGER NOT NULL DEFAULT 1,
            bloomberg_ticker TEXT
        )
        """
    )
    _ensure_companies_columns(conn)
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS document_paragraphs (
            paragraph_id TEXT NOT NULL PRIMARY KEY,
            doc_uid TEXT NOT NULL,
            ticker TEXT NOT NULL,
            doc_type TEXT NOT NULL,
            context_year INTEGER,
            quarter_label TEXT,
            para_index INTEGER NOT NULL,
            content TEXT NOT NULL
        )
        """
    )
    conn.execute(
        """
        CREATE INDEX IF NOT EXISTS idx_document_paragraphs_doc_uid
        ON document_paragraphs(doc_uid)
        """
    )
    conn.commit()


def replace_document_paragraphs(
    doc_uid: str,
    ticker: str,
    doc_type: str,
    *,
    context_year: int | None = None,
    quarter_label: str | None = None,
    records: list[tuple[str, int, str]],
) -> None:
    """
    替换某一文档下的全部段落：先按 ``doc_uid`` 删除再批量插入。

    ``records``：``(paragraph_id, para_index, content)``。
    """
    sym = (ticker or "").strip().upper()
    uid = (doc_uid or "").strip()
    if not sym or not uid or not records:
        return

    conn = get_connection()
    try:
        init_db(conn)
        conn.execute("DELETE FROM document_paragraphs WHERE doc_uid = ?", (uid,))
        conn.executemany(
            """
            INSERT INTO document_paragraphs (
                paragraph_id, doc_uid, ticker, doc_type,
                context_year, quarter_label, para_index, content
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
            """,
            [
                (
                    pid,
                    uid,
                    sym,
                    doc_type,
                    context_year,
                    quarter_label,
                    idx,
                    content,
                )
                for pid, idx, content in records
            ],
        )
        conn.commit()
    finally:
        conn.close()


def fetch_paragraph_texts_by_ids(ids: list[str]) -> dict[str, str]:
    """按段落 ID 批量取正文；缺失的 ID 不出现于结果中。"""
    want = [i.strip() for i in ids if i and str(i).strip()]
    if not want:
        return {}

    conn = get_connection()
    try:
        init_db(conn)
        placeholders = ",".join("?" * len(want))
        cur = conn.execute(
            f"SELECT paragraph_id, content FROM document_paragraphs "
            f"WHERE paragraph_id IN ({placeholders})",
            want,
        )
        return {str(r["paragraph_id"]): str(r["content"]) for r in cur.fetchall()}
    finally:
        conn.close()


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
