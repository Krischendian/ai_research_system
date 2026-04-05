"""财务抓取 + SQLite：需网络；从项目根目录执行：

PYTHONPATH=src python tests/test_financials.py
"""
from pprint import pprint

from research_automation.core.database import (
    get_connection,
    init_db,
    read_financials,
    save_financials,
)
from research_automation.extractors.yahoo_finance import get_financials


def main() -> None:
    ticker = "AAPL"

    data = get_financials(ticker)
    print(f"[抓取] 共 {len(data)} 条年度记录")
    for row in data:
        pprint(row.model_dump())

    save_financials(ticker, data)
    save_financials(ticker, data)
    print("\n[写入] 已 save_financials 两次（同内容应触发 upsert，不增加行数）")

    conn = get_connection()
    try:
        init_db(conn)
        n = conn.execute(
            "SELECT COUNT(*) AS c FROM financials WHERE ticker = ?",
            (ticker.upper(),),
        ).fetchone()["c"]
        print(f"[库内] ticker={ticker} 行数: {n}")
    finally:
        conn.close()

    loaded = read_financials(ticker)
    print(f"\n[读取] read_financials 共 {len(loaded)} 条")
    for row in loaded:
        pprint(row.model_dump())


if __name__ == "__main__":
    main()
