#!/usr/bin/env python3
"""将「AI 岗位替代」监控池标的写入 ``companies`` 表（SQLite ``data/research.db``）。

执行：
    PYTHONPATH=src python scripts/seed_ai_job_replacement.py

``ticker`` 与列表一致（含空格的非美代码，如 ``BT/A LN``）；首次插入时
``company_name`` 为空串、``bloomberg_ticker`` 为 NULL；已存在则仅更新
``sector`` 与 ``is_active``。
"""
from __future__ import annotations

import sys
from pathlib import Path

# 与 ``batch_fetch_financials.py`` 一致：支持无 PYTHONPATH 时从项目根执行
_ROOT = Path(__file__).resolve().parents[1]
_SRC = _ROOT / "src"
if str(_SRC) not in sys.path:
    sys.path.insert(0, str(_SRC))

from research_automation.core.database import get_connection, init_db

SECTOR = "AI_Job_Replacement"

# (ticker 原样规范化：strip + upper，保留空格与 /)，country
_ROWS: list[tuple[str, str]] = [
    ("CTSH", "US"),
    ("AVY", "US"),
    ("IBM", "US"),
    ("PPG", "US"),
    ("JLL", "US"),
    ("ACN", "US"),
    ("EL", "US"),
    ("TGT", "US"),
    ("UPS", "US"),
    ("DG", "US"),
    ("HCA", "US"),
    ("BAH", "US"),
    ("MDB", "US"),
    ("ZM", "US"),
    ("RTO", "US"),
    ("BT/A LN", "GB"),
    ("FRE GY", "DE"),
    ("KBX GY", "DE"),
    ("DHL GY", "DE"),
]


def _norm_ticker(raw: str) -> str:
    return (raw or "").strip().upper()


def main() -> None:
    conn = get_connection()
    inserted = 0
    updated = 0
    try:
        init_db(conn)
        sql_insert = """
            INSERT INTO companies (
                ticker, company_name, sector, country, is_active, bloomberg_ticker
            )
            VALUES (?, '', ?, ?, 1, NULL)
            ON CONFLICT(ticker) DO UPDATE SET
                sector = excluded.sector,
                is_active = 1
        """
        for raw_ticker, country in _ROWS:
            sym = _norm_ticker(raw_ticker)
            if not sym:
                continue
            existed = (
                conn.execute(
                    "SELECT 1 FROM companies WHERE ticker = ?",
                    (sym,),
                ).fetchone()
                is not None
            )
            conn.execute(sql_insert, (sym, SECTOR, country))
            if existed:
                updated += 1
            else:
                inserted += 1
        conn.commit()
    finally:
        conn.close()

    print(f"AI Job Replacement 池：插入 {inserted} 行，更新 {updated} 行（共 {inserted + updated}）。")


if __name__ == "__main__":
    main()
