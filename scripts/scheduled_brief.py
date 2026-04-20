#!/usr/bin/env python3
r"""
每日简报定时任务
建议 cron 配置（纽约时间 08:05，UTC 12:05）：
    5 12 * * 1-5 cd /Users/krisfan/Desktop/untitled\ folder && PYTHONPATH=src /path/to/venv/bin/python3 scripts/scheduled_brief.py >> logs/brief_cron.log 2>&1
"""
from __future__ import annotations

import logging
import os
import sys
from datetime import datetime, timezone, timedelta
from pathlib import Path

# 确保 src 在 path 里
_root = Path(__file__).resolve().parent.parent
_src = _root / "src"
for p in (_root, _src):
    if str(p) not in sys.path:
        sys.path.insert(0, str(p))

from dotenv import load_dotenv
load_dotenv(_root / ".env", override=False)

# 日志输出到 logs/brief_cron.log
_log_dir = _root / "logs"
_log_dir.mkdir(exist_ok=True)
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    handlers=[
        logging.StreamHandler(sys.stdout),
    ],
)
logger = logging.getLogger("scheduled_brief")


def _get_all_sectors() -> list[tuple[str, list[str]]]:
    """从数据库读取所有活跃 sector 及其 ticker 列表。"""
    from research_automation.core.database import get_connection, init_db
    conn = get_connection()
    try:
        init_db(conn)
        cur = conn.execute(
            "SELECT DISTINCT sector FROM companies WHERE is_active=1 AND TRIM(sector)!='' ORDER BY sector"
        )
        sectors = [str(r[0]).strip() for r in cur.fetchall() if r[0]]
        result = []
        for sec in sectors:
            cur2 = conn.execute(
                "SELECT ticker FROM companies WHERE is_active=1 AND TRIM(sector)=? ORDER BY ticker",
                (sec,),
            )
            tickers = [str(r[0]).strip() for r in cur2.fetchall() if r[0]]
            result.append((sec, tickers))
        return result
    finally:
        conn.close()


def run() -> None:
    ny_now = datetime.now(timezone(timedelta(hours=-4)))
    logger.info("=== 每日简报定时任务启动 纽约时间=%s ===", ny_now.strftime("%Y-%m-%d %H:%M EDT"))

    from research_automation.services.daily_brief_service import generate_daily_brief

    sectors = _get_all_sectors()
    if not sectors:
        logger.warning("数据库中无活跃 sector，退出。")
        return

    logger.info("共 %d 个 sector：%s", len(sectors), [s for s, _ in sectors])

    success = 0
    failed = 0
    for sector, tickers in sectors:
        logger.info("生成简报 sector=%s tickers=%d 个", sector, len(tickers))
        try:
            brief = generate_daily_brief(sector, tickers, force_refresh=True)
            char_count = len(brief)
            logger.info("sector=%s 完成，简报长度=%d 字符", sector, char_count)
            success += 1
        except Exception:
            logger.exception("sector=%s 简报生成失败", sector)
            failed += 1

    logger.info("=== 定时任务完成 成功=%d 失败=%d ===", success, failed)


if __name__ == "__main__":
    run()
