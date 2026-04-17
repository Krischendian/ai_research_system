#!/usr/bin/env python3
"""POC：从 ``companies`` 表读取 AI_Job_Replacement 标的，用 Tavily 拉取新闻信号并打印统计。

运行（需 ``TAVILY_API_KEY`` 与 ``data/research.db``）::

    PYTHONPATH=src python scripts/test_tavily_signals.py
"""
from __future__ import annotations

import os
import sys
from collections import Counter
from pathlib import Path

_ROOT = Path(__file__).resolve().parents[1]
_SRC = _ROOT / "src"
if str(_SRC) not in sys.path:
    sys.path.insert(0, str(_SRC))

from dotenv import load_dotenv  # noqa: E402

load_dotenv(_ROOT / ".env", override=False)

from research_automation.core.company_manager import list_companies  # noqa: E402
from research_automation.services.signal_fetcher import fetch_signals_for_ticker  # noqa: E402

SECTOR = "AI_Job_Replacement"
MAX_TICKERS = 5
DAYS_BACK = 7


def main() -> None:
    if not (os.getenv("TAVILY_API_KEY") or "").strip():
        print("错误：未设置 TAVILY_API_KEY（请写入 .env 后重试）")
        sys.exit(1)

    rows = list_companies(sector=SECTOR, active_only=True)
    if not rows:
        print(f"未在 companies 表找到 sector={SECTOR!r} 的活跃标的；可先运行 scripts/seed_ai_job_replacement.py")
        sys.exit(2)

    sample = rows[:MAX_TICKERS]
    print(
        f"sector={SECTOR!r} 活跃标的共 {len(rows)} 家；本次取前 {len(sample)} 家："
        f" {', '.join(r.ticker for r in sample)}\n"
    )

    type_counts: Counter[str] = Counter()

    for r in sample:
        t = r.ticker
        signals = fetch_signals_for_ticker(
            t,
            days_back=DAYS_BACK,
            company_name=(r.company_name or "").strip() or None,
        )
        print(f"=== {t} === 信号条数: {len(signals)}")
        for s in signals:
            st = str(s.get("signal_type") or "other")
            type_counts[st] += 1
            print(
                f"  [{st}] {s.get('title') or '(no title)'}\n"
                f"       url={s.get('url')}\n"
                f"       axis={s.get('query_axis')}  date={s.get('published_date')}"
            )
            prev = (s.get("content") or "")[:200].replace("\n", " ")
            if prev:
                print(f"       excerpt: {prev}…")
        if not signals:
            print("  （无命中：可能窗口内无相关报道，或均被分析师关键词过滤）")
        print()

    print("--- 汇总：signal_type ---")
    for k, v in type_counts.most_common():
        print(f"  {k}: {v}")
    if not type_counts:
        print("  （全部为 0；可放宽 days_back 或检查域名白名单是否过窄）")


if __name__ == "__main__":
    main()
