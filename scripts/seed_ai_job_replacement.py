#!/usr/bin/env python3
"""将「AI 岗位替代」监控池写入 ``companies`` 表（``data/research.db``）。

执行：
    PYTHONPATH=src python scripts/seed_ai_job_replacement.py

写入/更新：`ticker`、`company_name`、`country`、`sector`、`bloomberg_ticker`、`is_active`。

若库里仍存在旧代码（``BT/A LN`` / ``FRE GY`` / ``KBX GY`` / ``DHL GY``），会先删除，
避免与新简码重复出现在同一 sector。

对齐 Excel / Bloomberg：**展示用代码**建议使用 ``bloomberg_ticker`` 列（如 ``DHL GY Equity``）；
逻辑主键仍为 ``ticker``（简码）。
"""
from __future__ import annotations

import sys
from pathlib import Path

_ROOT = Path(__file__).resolve().parents[1]
_SRC = _ROOT / "src"
if str(_SRC) not in sys.path:
    sys.path.insert(0, str(_SRC))

from research_automation.core.database import get_connection, init_db

SECTOR = "AI_Job_Replacement"

# 与旧种子冲突时删除的旧主键（已改为 FRE / KBX / DHL / BT/A）
_SUPERSEDED_TICKERS: tuple[str, ...] = ("BT/A LN", "FRE GY", "KBX GY", "DHL GY")

_ROWS: list[dict[str, str]] = [
    {
        "ticker": "CTSH",
        "company_name": "Cognizant Technology Solutions",
        "country": "US",
        "bloomberg_ticker": "CTSH US Equity",
    },
    {
        "ticker": "AVY",
        "company_name": "Avery Dennison",
        "country": "US",
        "bloomberg_ticker": "AVY US Equity",
    },
    {
        "ticker": "IBM",
        "company_name": "IBM",
        "country": "US",
        "bloomberg_ticker": "IBM US Equity",
    },
    {
        "ticker": "PPG",
        "company_name": "PPG Industries",
        "country": "US",
        "bloomberg_ticker": "PPG US Equity",
    },
    {
        "ticker": "JLL",
        "company_name": "Jones Lang LaSalle",
        "country": "US",
        "bloomberg_ticker": "JLL US Equity",
    },
    {
        "ticker": "ACN",
        "company_name": "Accenture",
        "country": "US",
        "bloomberg_ticker": "ACN US Equity",
    },
    {
        "ticker": "EL",
        "company_name": "Estee Lauder",
        "country": "US",
        "bloomberg_ticker": "EL US Equity",
    },
    {
        "ticker": "TGT",
        "company_name": "Target",
        "country": "US",
        "bloomberg_ticker": "TGT US Equity",
    },
    {
        "ticker": "UPS",
        "company_name": "United Parcel Service",
        "country": "US",
        "bloomberg_ticker": "UPS US Equity",
    },
    {
        "ticker": "DG",
        "company_name": "Dollar General",
        "country": "US",
        "bloomberg_ticker": "DG US Equity",
    },
    {
        "ticker": "HCA",
        "company_name": "HCA Healthcare",
        "country": "US",
        "bloomberg_ticker": "HCA US Equity",
    },
    {
        "ticker": "BAH",
        "company_name": "Booz Allen Hamilton",
        "country": "US",
        "bloomberg_ticker": "BAH US Equity",
    },
    {
        "ticker": "MDB",
        "company_name": "MongoDB",
        "country": "US",
        "bloomberg_ticker": "MDB US Equity",
    },
    {
        "ticker": "ZM",
        "company_name": "Zoom Video Communications",
        "country": "US",
        "bloomberg_ticker": "ZM US Equity",
    },
    {
        "ticker": "RTO",
        "company_name": "Rentokil Initial",
        "country": "US",
        "bloomberg_ticker": "RTO US Equity",
    },
    {
        "ticker": "BT/A",
        "company_name": "BT Group",
        "country": "GB",
        "bloomberg_ticker": "BT/A LN Equity",
    },
    {
        "ticker": "FRE",
        "company_name": "Fresenius",
        "country": "DE",
        "bloomberg_ticker": "FRE GY Equity",
    },
    {
        "ticker": "KBX",
        "company_name": "Knorr-Bremse",
        "country": "DE",
        "bloomberg_ticker": "KBX GY Equity",
    },
    {
        "ticker": "DHL",
        "company_name": "DHL Group",
        "country": "DE",
        "bloomberg_ticker": "DHL GY Equity",
    },
]


def _norm_ticker(raw: str) -> str:
    return (raw or "").strip().upper()


def main() -> None:
    conn = get_connection()
    inserted = 0
    updated = 0
    try:
        init_db(conn)
        for old in _SUPERSEDED_TICKERS:
            conn.execute("DELETE FROM companies WHERE ticker = ?", (old,))
        conn.commit()

        sql_insert = """
            INSERT INTO companies (
                ticker, company_name, sector, country, is_active, bloomberg_ticker
            )
            VALUES (?, ?, ?, ?, 1, ?)
            ON CONFLICT(ticker) DO UPDATE SET
                company_name = excluded.company_name,
                sector = excluded.sector,
                country = excluded.country,
                is_active = 1,
                bloomberg_ticker = excluded.bloomberg_ticker
        """
        for row in _ROWS:
            sym = _norm_ticker(row["ticker"])
            if not sym:
                continue
            name = (row.get("company_name") or "").strip()
            ctry = (row.get("country") or "").strip()
            bb = (row.get("bloomberg_ticker") or "").strip() or None
            existed = (
                conn.execute(
                    "SELECT 1 FROM companies WHERE ticker = ?",
                    (sym,),
                ).fetchone()
                is not None
            )
            conn.execute(
                sql_insert,
                (sym, name, SECTOR, ctry, bb),
            )
            if existed:
                updated += 1
            else:
                inserted += 1
        conn.commit()
    finally:
        conn.close()

    print(
        f"AI Job Replacement 池：插入 {inserted} 行，更新 {updated} 行"
        f"（共 {inserted + updated}）；已尝试移除旧代码行：{_SUPERSEDED_TICKERS}。"
    )


if __name__ == "__main__":
    main()
