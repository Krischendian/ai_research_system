#!/usr/bin/env python3
"""
批量从 Yahoo Finance 抓取财务数据并写入 SQLite ``financials`` 表。

默认处理 ``companies`` 表中 ``is_active=1`` 的全部 ticker；
可用 ``--ticker`` 只更新单一标的；``--force`` 会忽略本地已有数据并重新抓取。
"""
from __future__ import annotations

import argparse
import sys
from pathlib import Path

# 保证从项目根执行 ``python scripts/batch_fetch_financials.py`` 时能导入研究自动化包
_ROOT = Path(__file__).resolve().parents[1]
_SRC = _ROOT / "src"
if str(_SRC) not in sys.path:
    sys.path.insert(0, str(_SRC))

from research_automation.core.company_manager import get_active_tickers  # noqa: E402
from research_automation.core.database import read_financials, save_financials  # noqa: E402
from research_automation.extractors.yahoo_finance import get_financials  # noqa: E402


def _parse_args() -> argparse.Namespace:
    """解析命令行参数。"""
    p = argparse.ArgumentParser(description="批量抓取 Yahoo 财务数据并写入 financials 表")
    p.add_argument(
        "--ticker",
        type=str,
        default=None,
        help="仅更新指定股票代码（如 AAPL），不必在 companies 表中",
    )
    p.add_argument(
        "--force",
        action="store_true",
        help="强制重新抓取并覆盖该 ticker 在库中年份的已有数据",
    )
    return p.parse_args()


def _resolve_tickers(single: str | None) -> list[str]:
    """得到待处理的 ticker 列表：单标的模式或全部活跃标的。"""
    if single is None:
        return get_active_tickers()
    s = single.strip().upper()
    if not s:
        return []  # 显式传入空代码则交由 main 提示错误
    return [s]


def run_batch_fetch(*, ticker: str | None = None, force: bool = False) -> int:
    """
    批量抓取财务并入库（供 CLI 与 APScheduler 共用）。

    返回 0=全部成功或跳过；1=无 ticker；2=部分失败。
    """
    tickers = _resolve_tickers(ticker)

    if not tickers:
        print("没有可处理的 ticker：请先在 companies 表添加活跃公司，或使用 --ticker")
        return 1

    failures: list[tuple[str, str]] = []

    for sym in tickers:
        print(f"正在处理 {sym}...", end=" ", flush=True)
        try:
            # 非 force 时若库中已有该标的的年度行，则跳过以减少无效请求
            if not force and read_financials(sym):
                print("跳过（已有数据，使用 --force 可覆盖）")
                continue

            rows = get_financials(sym)
            if not rows:
                msg = "未取到有效财务数据"
                failures.append((sym, msg))
                print(f"失败：{msg}")
                continue

            save_financials(sym, rows)
            print("成功")
        except Exception as exc:  # noqa: BLE001
            # 单只标的异常不中断批量流程，记入失败列表
            err = str(exc)
            failures.append((sym, err))
            print(f"失败：{err}")

    if failures:
        print("\n--- 失败汇总 ---")
        for t, reason in failures:
            print(f"  {t}: {reason}")
        return 2

    return 0


def main() -> int:
    args = _parse_args()
    return run_batch_fetch(ticker=args.ticker, force=args.force)


if __name__ == "__main__":
    raise SystemExit(main())