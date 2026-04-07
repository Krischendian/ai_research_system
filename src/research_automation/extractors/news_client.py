"""从 Reuters / Bloomberg 等平台 RSS 拉取新闻（原始条目，未经过 LLM）。"""
from __future__ import annotations

import logging
import re
from datetime import datetime, timezone
from email import utils as email_utils
from typing import TypedDict

from typing_extensions import NotRequired

import feedparser
import requests
from bs4 import BeautifulSoup

logger = logging.getLogger(__name__)

# 公司名称常见后缀，用于生成别名以做简单子串匹配（英文稿）
_COMPANY_SUFFIX_PAT = re.compile(
    r",?\s*(Inc\.?|Incorporated|Corp\.?|Corporation|Ltd\.?|PLC|plc)\s*$",
    re.I,
)

# 中文摘要里常见写法 ↔ ticker（仅作关键词命中；与监控池取交集后生效）
_ZH_TICKER_HINTS: dict[str, tuple[str, ...]] = {
    "AAPL": ("苹果公司", "苹果",),
    "MSFT": ("微软", "微软公司"),
    "GOOGL": ("谷歌", "字母公司"),
    "GOOG": ("谷歌",),
    "AMZN": ("亚马逊",),
    "META": ("脸书", "脸书公司", "meta平台"),
    "NVDA": ("英伟达", "辉达"),
    "TSLA": ("特斯拉",),
    "JPM": ("摩根大通", "小摩"),
}

DEFAULT_HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
        "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"
    ),
}

# （URL, 在 source 字段中展示的标签）
# 科技/行业类源置前，便于「公司新闻」命中 ticker；地缘类仍在列表后部。
RSS_FEEDS: list[tuple[str, str]] = [
    ("https://feeds.bloomberg.com/technology/news.rss", "Bloomberg"),
    ("https://feeds.bloomberg.com/industries/news.rss", "Bloomberg"),
    ("https://techcrunch.com/feed/", "TechCrunch"),
    ("https://www.reuters.com/rssfeed/businessNews", "Reuters"),
    ("https://feeds.bloomberg.com/markets/news.rss", "Bloomberg"),
    ("https://www.reuters.com/rssfeed/worldNews", "Reuters"),
    ("https://feeds.bloomberg.com/economics/news.rss", "Bloomberg"),
    ("https://feeds.bloomberg.com/politics/news.rss", "Bloomberg"),
]


class RawArticle(TypedDict):
    """RSS 单条原文。"""

    title: str
    link: str
    description: str
    source: str
    # 发布时间（UTC），ISO8601；部分条目无则不含此键
    published_at_utc: NotRequired[str]


def _struct_time_to_utc(st: object) -> datetime | None:
    """将 feedparser 的 time_struct 转为 UTC aware datetime。"""
    if st is None:
        return None
    try:
        y, m, d, H, M, S = (
            int(st.tm_year),
            int(st.tm_mon),
            int(st.tm_mday),
            int(st.tm_hour),
            int(st.tm_min),
            int(st.tm_sec),
        )
        return datetime(y, m, d, H, M, S, tzinfo=timezone.utc)
    except (AttributeError, TypeError, ValueError):
        return None


def _entry_published_utc(entry: object) -> datetime | None:
    """从 feedparser 单条 entry 解析 UTC 发布时间。"""
    get = getattr(entry, "get", None)
    if not callable(get):
        return None
    t = get("published_parsed") or get("updated_parsed")
    dt = _struct_time_to_utc(t)
    if dt is not None:
        return dt
    raw = get("published") or get("updated")
    if isinstance(raw, str) and raw.strip():
        try:
            dtp = email_utils.parsedate_to_datetime(raw.strip())
            if dtp.tzinfo is None:
                dtp = dtp.replace(tzinfo=timezone.utc)
            else:
                dtp = dtp.astimezone(timezone.utc)
            return dtp
        except (TypeError, ValueError):
            return None
    return None


def parse_published_at_utc(iso: str) -> datetime:
    """
    将 ``published_at_utc``（ISO8601，可能以 Z 结尾）解析为 UTC 下的 aware datetime。
    """
    t = (iso or "").strip()
    if t.endswith("Z"):
        t = t[:-1] + "+00:00"
    dt = datetime.fromisoformat(t)
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(timezone.utc)


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
        art: RawArticle = {
            "title": title,
            "link": link,
            "description": desc,
            "source": label,
        }
        pub = _entry_published_utc(entry)
        if pub is not None:
            art["published_at_utc"] = pub.astimezone(timezone.utc).replace(
                microsecond=0
            ).isoformat().replace("+00:00", "Z")
        items.append(art)
    return items


def _company_name_variants(legal_name: str) -> set[str]:
    """从数据库中的法定/展示名称扩展出若干用于匹配的短形式。"""
    n = (legal_name or "").strip()
    out: set[str] = set()
    if not n:
        return out
    out.add(n.lower())
    base = _COMPANY_SUFFIX_PAT.sub("", n).strip()
    if base:
        out.add(base.lower())
    first = n.split(",")[0].strip()
    if first:
        out.add(first.lower())
        out.add(_COMPANY_SUFFIX_PAT.sub("", first).strip().lower())
    return {x for x in out if len(x) >= 2}


def extract_tickers_from_text(text: str) -> list[str]:
    """
    从正文做简单关键词匹配，找出**当前监控池（companies 表 is_active=1）**中涉及的 ticker。

    规则：``$AAPL`` cashtag、独立单词形式代码（大小写不敏感边界）、
    公司全名/短名子串（来自 ``company_name``，不区分大小写）、
    以及预置中文简称（如「苹果公司」→ ``AAPL``），便于中文 LLM 摘要命中。

    返回去重、按字母序排序的大写 ticker 列表。
    """
    if not (text or "").strip():
        return []

    from research_automation.core.company_manager import list_companies

    companies = list_companies(active_only=True)
    active: set[str] = {c.ticker.strip().upper() for c in companies if c.ticker.strip()}
    if not active:
        logger.debug("extract_tickers_from_text: 无活跃公司，跳过匹配")
        return []

    found: set[str] = set()
    t_full = text
    t_upper = t_full.upper()
    t_lower = t_full.lower()

    for m in re.finditer(r"\$\s*([A-Za-z]{1,5})\b", text):
        sym = m.group(1).upper()
        if sym in active:
            found.add(sym)

    for sym in active:
        try:
            if re.search(
                rf"(?<![A-Za-z0-9]){re.escape(sym)}(?![A-Za-z0-9])",
                t_full,
                flags=re.IGNORECASE,
            ):
                found.add(sym)
        except re.error:
            continue

    for c in companies:
        sym = c.ticker.strip().upper()
        if sym not in active:
            continue
        for variant in _company_name_variants(c.company_name):
            if len(variant) >= 3 and variant in t_lower:
                found.add(sym)
                break

    # 中文 / 本地媒体常用简称（与 LLM 中文摘要对齐）
    for sym in active:
        hints = _ZH_TICKER_HINTS.get(sym)
        if not hints:
            continue
        for h in hints:
            if len(h) >= 2 and h.lower() in t_lower:
                found.add(sym)
                break

    return sorted(found)


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
