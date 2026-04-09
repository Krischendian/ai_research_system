"""
SEC 10-K 财务表格解析冒烟测试：需网络；从项目根执行：

PYTHONPATH=src SEC_EDGAR_USER_AGENT="YourApp/1.0 (you@example.com)" python tests/test_sec_financials.py
"""
from __future__ import annotations

from datetime import datetime, timezone

from research_automation.extractors.sec_edgar import get_financial_statements


def main() -> None:
    ticker = "AAPL"
    cy = datetime.now(timezone.utc).year
    # 使用已结束的三个公历申报年，减少「当年尚无 10-K」与回退导致的重复命中
    years = [cy - 1, cy - 2, cy - 3]
    print(f"标的 {ticker}，申报公历年 {years}（与 get_10k_text 的 year 含义一致）\n")
    for y in years:
        row = get_financial_statements(ticker, y)
        print(f"--- filing_year={y} ---")
        if row is None:
            print("  (解析失败或暂无 10-K)")
        else:
            print(f"  {row.model_dump()}")


if __name__ == "__main__":
    main()
