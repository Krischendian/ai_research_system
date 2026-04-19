"""
每日新闻简报服务：
- 宏观新闻：Bloomberg RSS（按sector关键词过滤）
- 公司新闻：Benzinga API（按ticker精准拉取）
- LLM：对两类新闻分别提炼要点
两个时间窗口：隔夜（纽约时间16:00→次日08:00）+ 昨日全天
"""
from __future__ import annotations

import hashlib
import logging
import os
import sqlite3
from datetime import datetime, timedelta, timezone
from typing import Any

import requests
from dotenv import load_dotenv
from pathlib import Path

from research_automation.core.sector_config import get_sector_macro_keywords
from research_automation.extractors.bloomberg_rss_client import _parse_rss_items
from research_automation.extractors.llm_client import chat

load_dotenv(Path(__file__).resolve().parents[3] / ".env", override=False)
logger = logging.getLogger(__name__)

# ── 每日简报缓存（SQLite） ─────────────────────────────────────────
_CACHE_DB_PATH = Path(__file__).resolve().parents[3] / "daily_brief_cache.db"


def _get_cache_conn() -> sqlite3.Connection:
    conn = sqlite3.connect(str(_CACHE_DB_PATH))
    conn.execute("""
        CREATE TABLE IF NOT EXISTS daily_brief_cache (
            cache_key  TEXT PRIMARY KEY,
            sector     TEXT NOT NULL,
            date_str   TEXT NOT NULL,
            content    TEXT NOT NULL,
            created_at TEXT NOT NULL
        )
    """)
    conn.commit()
    return conn


def _cache_key(sector: str, date_str: str) -> str:
    raw = f"{sector}::{date_str}"
    return hashlib.md5(raw.encode()).hexdigest()


def _get_brief_cache(sector: str, date_str: str) -> str | None:
    try:
        conn = _get_cache_conn()
        key = _cache_key(sector, date_str)
        row = conn.execute(
            "SELECT content FROM daily_brief_cache WHERE cache_key=?", (key,)
        ).fetchone()
        conn.close()
        return row[0] if row else None
    except Exception as e:
        logger.warning("读取每日简报缓存失败: %s", e)
        return None


def _save_brief_cache(sector: str, date_str: str, content: str) -> None:
    try:
        conn = _get_cache_conn()
        key = _cache_key(sector, date_str)
        now = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC")
        conn.execute(
            """
            INSERT OR REPLACE INTO daily_brief_cache
                (cache_key, sector, date_str, content, created_at)
            VALUES (?, ?, ?, ?, ?)
            """,
            (key, sector, date_str, content, now),
        )
        conn.commit()
        conn.close()
    except Exception as e:
        logger.warning("写入每日简报缓存失败: %s", e)


def _delete_brief_cache(sector: str, date_str: str) -> None:
    try:
        conn = _get_cache_conn()
        key = _cache_key(sector, date_str)
        conn.execute("DELETE FROM daily_brief_cache WHERE cache_key=?", (key,))
        conn.commit()
        conn.close()
    except Exception as e:
        logger.warning("删除每日简报缓存失败: %s", e)


# ── 缓存工具 END ──────────────────────────────────────────────────

BENZINGA_BASE = "https://api.massive.com/benzinga/v2"
BLOOMBERG_FEEDS = [
    "https://feeds.bloomberg.com/markets/news.rss",
    "https://feeds.bloomberg.com/technology/news.rss",
    "https://feeds.bloomberg.com/politics/news.rss",
]

# 过滤低质量来源
_BLOCKED_DOMAINS = {"weibo.com", "wechat.com", "sina.com", "sohu.com", "163.com"}


def _get_bz_key() -> str | None:
    return (os.environ.get("BENZINGA_API_KEY") or "").strip() or None


def _ny_windows() -> tuple[tuple[datetime, datetime], tuple[datetime, datetime]]:
    """返回隔夜和昨日两个UTC时间窗口。"""
    # 用UTC-4近似纽约时间（EDT）
    ny_offset = timedelta(hours=-4)
    now_ny = datetime.now(timezone.utc).astimezone(timezone(ny_offset))
    today_ny = now_ny.replace(hour=0, minute=0, second=0, microsecond=0)
    yesterday_ny = today_ny - timedelta(days=1)

    overnight = (
        (yesterday_ny.replace(hour=16)).astimezone(timezone.utc),
        (today_ny.replace(hour=8)).astimezone(timezone.utc),
    )
    yesterday = (
        yesterday_ny.astimezone(timezone.utc),
        (yesterday_ny.replace(hour=23, minute=59, second=59)).astimezone(timezone.utc),
    )
    return overnight, yesterday


def _fetch_bloomberg_rss(from_dt: datetime, to_dt: datetime) -> list[dict[str, Any]]:
    """从Bloomberg RSS抓取指定时间窗口内的新闻。"""
    all_items: list[dict[str, Any]] = []
    seen_urls: set[str] = set()
    for feed_url in BLOOMBERG_FEEDS:
        try:
            r = requests.get(
                feed_url, timeout=15,
                headers={"User-Agent": "research-automation/1.0"}
            )
            if r.status_code != 200:
                continue
            items = _parse_rss_items(r.text)
            for item in items:
                url = item.get("url", "")
                if url in seen_urls:
                    continue
                # 时间过滤
                pub = item.get("published_date")
                if pub:
                    try:
                        dt = datetime.fromisoformat(pub.replace("Z", "+00:00"))
                        if not (from_dt <= dt <= to_dt):
                            continue
                    except Exception:
                        pass
                seen_urls.add(url)
                all_items.append(item)
        except Exception as e:
            logger.warning("Bloomberg RSS失败 %s: %s", feed_url, e)
    return all_items


def _fetch_benzinga_company_news(
    tickers: list[str],
    from_dt: datetime,
    to_dt: datetime,
    page_size: int = 50,
) -> list[dict[str, Any]]:
    """从Benzinga API按ticker拉取公司新闻。"""
    key = _get_bz_key()
    if not key:
        return []
    us_tickers = [t for t in tickers if " " not in t and "." not in t]
    if not us_tickers:
        return []
    try:
        r = requests.get(
            f"{BENZINGA_BASE}/news",
            params={
                "apiKey": key,
                "pageSize": page_size,
                "tickers": ",".join(us_tickers[:30]),
                "dateFrom": from_dt.strftime("%Y-%m-%dT%H:%M:%S"),
                "dateTo": to_dt.strftime("%Y-%m-%dT%H:%M:%S"),
            },
            timeout=30,
        )
        if r.status_code != 200:
            return []
        data = r.json()
        return data.get("results", []) if isinstance(data, dict) else []
    except Exception as e:
        logger.warning("Benzinga公司新闻失败: %s", e)
        return []


def _filter_macro_by_sector(
    items: list[dict[str, Any]],
    sector: str,
) -> list[dict[str, Any]]:
    """用sector关键词过滤Bloomberg宏观新闻，只保留相关条目。"""
    keywords = get_sector_macro_keywords(sector)
    result = []
    seen = set()
    for item in items:
        url = item.get("url", "")
        if url in seen:
            continue
        blob = f"{item.get('title','')} {item.get('content','')}".lower()
        if any(kw in blob for kw in keywords):
            seen.add(url)
            result.append(item)
    return result


def _llm_summarize_macro(items: list[dict[str, Any]], sector: str) -> str:
    """用LLM提炼宏观新闻要点。"""
    if not items:
        return "*暂无相关宏观新闻*"
    news_text = "\n".join(
        f"- {item.get('title','')}：{item.get('content','')[:200]}"
        for item in items[:10]
    )
    prompt = f"""你是金融分析师助手。以下是与{sector}板块相关的宏观新闻：

{news_text}

请提炼3-5条对{sector}板块投资最有价值的宏观要点，每条一行，格式：
- [要点]（来源：[新闻标题简称]）

要求：
1. 只提对投资决策有实际影响的信息
2. 避免泛泛而谈，要具体（数字、政策名称、地区）
3. 用中文输出"""
    try:
        return chat(prompt, max_tokens=500)
    except Exception as e:
        logger.warning("LLM宏观摘要失败: %s", e)
        return "*宏观摘要生成失败*"


def _llm_summarize_company(
    items: list[dict[str, Any]],
    sector: str,
    tickers: list[str],
) -> str:
    """用LLM提炼公司新闻要点。"""
    if not items:
        return "*暂无公司新闻*"

    # 去重
    seen_titles: set[str] = set()
    deduped = []
    for item in items:
        t = (item.get("title") or "").strip()
        if t and t not in seen_titles:
            seen_titles.add(t)
            deduped.append(item)

    news_text = "\n".join(
        f"- [{','.join(item.get('tickers',[]))}] {item.get('title','')}："
        f"{(item.get('teaser') or '')[:150]}"
        for item in deduped[:20]
    )
    prompt = f"""你是金融分析师助手。以下是{sector}板块的公司新闻：

{news_text}

请按公司分组，提炼每家公司最重要的1-2条信息，格式：
**[TICKER]** • [要点]

要求：
1. 只保留对投资有价值的信息（收购/裁员/财务/管理层变动/新产品）
2. 过滤掉股价涨跌、分析师评级等噪音
3. 用中文输出，保留关键英文术语"""
    try:
        return chat(prompt, max_tokens=800)
    except Exception as e:
        logger.warning("LLM公司摘要失败: %s", e)
        return "*公司摘要生成失败*"


def generate_daily_brief(
    sector: str,
    tickers: list[str],
    *,
    force_refresh: bool = False,
) -> str:
    """
    生成单个sector的每日新闻简报。
    返回Markdown字符串。
    """
    # ── 缓存读取 ──────────────────────────────────────────────────
    ny_today = datetime.now(timezone(timedelta(hours=-4))).strftime("%Y-%m-%d")
    if not force_refresh:
        cached = _get_brief_cache(sector, ny_today)
        if cached:
            logger.info("每日简报命中缓存 sector=%s date=%s", sector, ny_today)
            return cached
    # ── 缓存读取 END ──────────────────────────────────────────────

    overnight_window, yesterday_window = _ny_windows()
    ov_start, ov_end = overnight_window
    yd_start, yd_end = yesterday_window

    now_str = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC")
    ny_str = datetime.now(timezone(timedelta(hours=-4))).strftime("%Y-%m-%d %H:%M EDT")

    lines = [
        f"# 每日简报｜{sector}",
        f"**生成时间**：{now_str}（纽约：{ny_str}）",
        "",
    ]

    for window_label, from_dt, to_dt in [
        (f"📌 隔夜（{ov_start.strftime('%m/%d %H:%M')}-{ov_end.strftime('%H:%M')} UTC）", ov_start, ov_end),
        (f"📋 昨日回顾（{yd_start.strftime('%m/%d')} 全天）", yd_start, yd_end),
    ]:
        lines.append(f"## {window_label}")
        lines.append("")

        # 宏观新闻
        bloomberg_items = _fetch_bloomberg_rss(from_dt, to_dt)
        macro_filtered = _filter_macro_by_sector(bloomberg_items, sector)
        lines.append("### 🌍 宏观要点")
        lines.append("")
        macro_summary = _llm_summarize_macro(macro_filtered, sector)
        lines.append(macro_summary)
        lines.append("")

        # 原文链接
        if macro_filtered:
            lines.append("<details><summary>宏观新闻原文链接</summary>")
            lines.append("")
            for item in macro_filtered[:8]:
                title = item.get("title", "")
                url = item.get("url", "")
                if url:
                    lines.append(f"- [{title}]({url})")
            lines.append("")
            lines.append("</details>")
            lines.append("")

        # 公司新闻
        company_items = _fetch_benzinga_company_news(tickers, from_dt, to_dt)
        lines.append("### 🏢 公司要点")
        lines.append("")
        company_summary = _llm_summarize_company(company_items, sector, tickers)
        lines.append(company_summary)
        lines.append("")

        # 原文链接
        seen_titles: set[str] = set()
        if company_items:
            lines.append("<details><summary>公司新闻原文链接</summary>")
            lines.append("")
            for item in company_items[:15]:
                title = (item.get("title") or "").strip()
                url = item.get("url", "")
                ticker_str = ",".join(item.get("tickers", []))
                if title and title not in seen_titles and url:
                    lines.append(f"- [{ticker_str}] [{title}]({url})")
                    seen_titles.add(title)
            lines.append("")
            lines.append("</details>")
            lines.append("")

    # ── 缓存写入 ──────────────────────────────────────────────────
    result = "\n".join(lines).rstrip() + "\n"
    _save_brief_cache(sector, ny_today, result)
    # ── 缓存写入 END ──────────────────────────────────────────────
    return result
