"""从 Reuters / Bloomberg 等平台 RSS 拉取新闻（原始条目，未经过 LLM）。"""
from __future__ import annotations

import logging
from typing import TypedDict

import feedparser
import requests
from bs4 import BeautifulSoup

logger = logging.getLogger(__name__)

DEFAULT_HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
        "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"
    ),
}

# （URL, 在 source 字段中展示的标签）
RSS_FEEDS: list[tuple[str, str]] = [
    ("https://www.reuters.com/rssfeed/businessNews", "Reuters"),
    ("https://www.reuters.com/rssfeed/worldNews", "Reuters"),
    ("https://feeds.bloomberg.com/markets/news.rss", "Bloomberg"),
    ("https://feeds.bloomberg.com/economics/news.rss", "Bloomberg"),
    ("https://feeds.bloomberg.com/politics/news.rss", "Bloomberg"),
]


class RawArticle(TypedDict):
    """RSS 单条原文。"""

    title: str
    link: str
    description: str
    source: str


def _plain_text(html_or_text: str) -> str:
    if not html_or_text:
        return ""
    soup = BeautifulSoup(html_or_text, "lxml")
    text = soup.get_text(separator=" ", strip=True)
    return " ".join(text.split())


def _parse_feed(url: str, label: str) -> list[RawArticle]:
    items: list[RawArticle] = []
    try:
        resp = requests.get(url, timeout=20, headers=DEFAULT_HEADERS)
        resp.raise_for_status()
        parsed = feedparser.parse(resp.content)
    except Exception as exc:
        logger.warning("RSS 拉取失败 url=%s: %s", url, exc)
        return items

    for entry in getattr(parsed, "entries", []) or []:
        title = (entry.get("title") or "").strip()
        if not title:
            continue
        link = (entry.get("link") or "").strip()
        raw_desc = entry.get("summary") or entry.get("description") or ""
        desc = _plain_text(str(raw_desc))[:1200]
        items.append(
            RawArticle(
                title=title,
                link=link,
                description=desc,
                source=label,
            )
        )
    return items


def fetch_rss_articles(*, max_items: int = 24, per_feed_limit: int = 10) -> list[RawArticle]:
    """
    依次请求配置的 RSS，合并为去重后的列表（按标题小写去重）。

    单源失败不中断，尽量返回已抓到的条目；也可能返回空列表。
    """
    if max_items <= 0:
        return []

    seen: set[str] = set()
    out: list[RawArticle] = []

    for url, label in RSS_FEEDS:
        try:
            batch = _parse_feed(url, label)
        except Exception as exc:
            logger.warning("RSS 解析异常 url=%s: %s", url, exc)
            continue
        for art in batch[:per_feed_limit]:
            key = art["title"].lower()
            if key in seen:
                continue
            seen.add(key)
            out.append(art)
            if len(out) >= max_items:
                return out
    return out
