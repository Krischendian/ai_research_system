"""
Bloomberg RSS 新闻抓取（免费，无需 API Key）。
端点：https://feeds.bloomberg.com/markets/news.rss
支持按公司名关键词过滤。
"""
from __future__ import annotations

import logging
import re
from datetime import datetime, timezone
from typing import Any

import requests

logger = logging.getLogger(__name__)

_FEEDS = [
    "https://feeds.bloomberg.com/markets/news.rss",
    "https://feeds.bloomberg.com/technology/news.rss",
    "https://feeds.bloomberg.com/industries/news.rss",
]

_DEFAULT_TIMEOUT_SEC = 15.0


def _parse_rss_items(xml_text: str) -> list[dict[str, Any]]:
    """简单解析RSS XML，不依赖外部库。"""
    items = []
    item_blocks = re.findall(r"<item>(.*?)</item>", xml_text, re.DOTALL)
    for block in item_blocks:
        def extract(tag: str) -> str:
            m = re.search(rf"<{tag}[^>]*><!\[CDATA\[(.*?)\]\]></{tag}>", block, re.DOTALL)
            if m:
                return m.group(1).strip()
            m = re.search(rf"<{tag}[^>]*>(.*?)</{tag}>", block, re.DOTALL)
            if m:
                return m.group(1).strip()
            # 自闭合 / 仅 href：如 <link rel="alternate" href="https://..."/>
            m = re.search(
                rf"<{tag}[^>]+href=[\"']([^\"']+)[\"'][^>]*/?>",
                block,
                re.IGNORECASE | re.DOTALL,
            )
            return m.group(1).strip() if m else ""

        title = extract("title")
        # Bloomberg RSS 用 <guid> 作为真实URL，<link>格式特殊
        url = extract("guid") or extract("link")
        content = extract("description")
        pub = extract("pubDate")

        if not title or not url:
            continue

        # 解析pubDate
        pub_str = None
        if pub:
            try:
                from email.utils import parsedate_to_datetime

                dt = parsedate_to_datetime(pub).astimezone(timezone.utc)
                pub_str = dt.strftime("%Y-%m-%dT%H:%M:%S+00:00")
            except Exception:
                pub_str = pub[:32]

        items.append({
            "title": title,
            "url": url,
            "content": content,
            "published_date": pub_str,
        })
    return items


def search_news(
    query: str,
    days_back: int = 7,
    max_results: int = 10,
    **kwargs: Any,
) -> list[dict[str, Any]]:
    """
    从Bloomberg RSS抓取新闻，按query关键词过滤标题+摘要。
    无需API Key，失败返回空列表。
    """
    q = (query or "").strip().lower()
    keywords = [w for w in re.split(r"\s+", q) if len(w) >= 3]
    if not keywords:
        return []

    all_items: list[dict[str, Any]] = []
    seen_urls: set[str] = set()

    for feed_url in _FEEDS:
        try:
            r = requests.get(
                feed_url,
                timeout=_DEFAULT_TIMEOUT_SEC,
                headers={"User-Agent": "research-automation/1.0"},
            )
            if r.status_code != 200:
                continue
            items = _parse_rss_items(r.text)
            for item in items:
                url = item.get("url", "")
                if url in seen_urls:
                    continue
                blob = f"{item.get('title', '')} {item.get('content', '')}".lower()
                if any(kw in blob for kw in keywords):
                    seen_urls.add(url)
                    all_items.append(item)
        except Exception as e:
            logger.warning("Bloomberg RSS 请求失败 %s: %s", feed_url, e)
            continue

    return all_items[:max_results]
