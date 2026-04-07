"""公司列表管理测试：预置 5 家科技公司并打印列表。

项目根目录执行：
PYTHONPATH=src python tests/test_company_manager.py
"""
from __future__ import annotations

from research_automation.core.company_manager import (
    CompanyRecord,
    add_company,
    get_active_tickers,
    list_companies,
    seed_default_tech_companies,
)


def main() -> None:
    # 预置 /  upsert
    seed_default_tech_companies()

    # 额外断言：添加与查询
    add_company("NVDA", "NVIDIA Corporation", "Technology", "US", is_active=0)
    all_rows = list_companies()
    assert len(all_rows) >= 5, "至少应有预置的 5 家公司"

    tech = list_companies(sector="Technology")
    assert len(tech) >= 5

    active = list_companies(sector="Technology", active_only=True)
    tickers_active = {r.ticker for r in active}
    assert {"AAPL", "MSFT", "GOOGL", "AMZN", "META"}.issubset(tickers_active)
    assert "NVDA" not in tickers_active  # is_active=0

    tix = get_active_tickers()
    assert "AAPL" in tix and "NVDA" not in tix

    print("=== 全部公司（含非活跃）===")
    for r in sorted(all_rows, key=lambda x: x.ticker):
        _print_row(r)

    print("\n=== Technology 且监控中 ===")
    for r in list_companies(sector="Technology", active_only=True):
        _print_row(r)

    print("\n=== get_active_tickers() ===")
    print(tix)


def _print_row(r: CompanyRecord) -> None:
    print(
        f"  {r.ticker:6} | {r.company_name:28} | {r.sector:12} | "
        f"{r.country:4} | active={r.is_active}"
    )


if __name__ == "__main__":
    main()
