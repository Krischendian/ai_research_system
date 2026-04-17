#!/usr/bin/env python3
"""生成 ``AI_Job_Replacement`` 行业 Markdown 报告并写入 ``data/reports/``。

执行::

    PYTHONPATH=src python3 scripts/generate_sector_report.py

依赖：``data/research.db`` 中已有该 sector 活跃公司；可选 ``TAVILY_API_KEY``、``FMP_API_KEY``。
相关性阈值：环境变量 ``REPORT_RELEVANCE_THRESHOLD``（默认 ``1``），与 ``sector_report_service`` 一致。
"""
from __future__ import annotations

import os
import sys
from datetime import datetime, timezone
from pathlib import Path

_ROOT = Path(__file__).resolve().parents[1]
_SRC = _ROOT / "src"
if str(_SRC) not in sys.path:
    sys.path.insert(0, str(_SRC))

from dotenv import load_dotenv  # noqa: E402

load_dotenv(_ROOT / ".env", override=False)

from research_automation.services.sector_report_service import (  # noqa: E402
    generate_sector_report,
)
from research_automation.services.signal_fetcher import SignalFetchStats  # noqa: E402

SECTOR = "AI_Job_Replacement"
DAYS_BACK = 7


def _threshold_from_env() -> int | None:
    """若显式设置则传入 ``generate_sector_report``；否则由服务内默认读取 ``.env``。"""
    raw = (os.getenv("REPORT_RELEVANCE_THRESHOLD") or "").strip()
    if not raw:
        return None
    try:
        return max(0, min(3, int(raw)))
    except ValueError:
        return None


def main() -> None:
    stats: dict = {}
    thr = _threshold_from_env()
    md = generate_sector_report(
        SECTOR,
        days_back=DAYS_BACK,
        relevance_threshold=thr,
        report_stats=stats,
    )
    out_dir = _ROOT / "data" / "reports"
    out_dir.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now(timezone.utc).strftime("%Y%m%d")
    path = out_dir / f"ai_job_replacement_report_{stamp}.md"
    path.write_text(md, encoding="utf-8")
    print(md)

    # 控制台摘要：与报告末尾「调试统计」一致，便于流水线/CI 快速扫一眼
    print("\n--- 控制台统计 ---\n")
    fa = stats.get("fetch_aggregate")
    if isinstance(fa, SignalFetchStats):
        print(f"Tavily 原始行数: {fa.raw_row_count}")
        print(f"合并后唯一条数: {fa.unique_url_count}")
        print(f"近似去重行数: {fa.dropped_duplicate_rows}")
        print(f"过期丢弃(UTC): {fa.dropped_expired}")
        print(f"噪音丢弃: {fa.dropped_noise}")
        print(f"其他规则丢弃: {fa.dropped_other}")
    print(f"过 fetch 后条数合计: {stats.get('raw_signal_count')}")
    print(f"相关性阈值: {stats.get('relevance_threshold')}")
    print(f"低于阈值丢弃: {stats.get('below_relevance_dropped')}")
    print(f"跨标的重复 URL 跳过: {stats.get('cross_ticker_duplicate_urls')}")
    print(f"写入报告条数: {stats.get('filtered_signal_count')}")
    print(f"\n[已写入] {path}")


if __name__ == "__main__":
    main()
