"""
Tavily Search API：面向「新闻信号」的网页检索（不替代 Benzinga/Finnhub 公司新闻流）。

需环境变量 ``TAVILY_API_KEY``；失败或无密钥时返回空列表并打日志。
"""
from __future__ import annotations

import logging
import os
from pathlib import Path
from typing import Any

import requests
from dotenv import load_dotenv

logger = logging.getLogger(__name__)

load_dotenv(Path(__file__).resolve().parents[3] / ".env", override=False)

TAVILY_SEARCH_URL = "https://api.tavily.com/search"
_DEFAULT_TIMEOUT_SEC = 45.0
_DEFAULT_INCLUDE_DOMAINS = [
    "bloomberg.com",
    "reuters.com",
    "wsj.com",
    "benzinga.com",
    "ft.com",
]


def _api_key() -> str | None:
    k = (os.getenv("TAVILY_API_KEY") or "").strip()
    return k or None


def _time_range_for_days(days_back: int) -> str:
    """Tavily ``time_range`` 仅为枚举字符串；按 ``days_back`` 取最接近一档。"""
    d = max(1, int(days_back))
    if d <= 1:
        return "day"
    if d <= 3:
        return "week"
    # 实测 ``week`` + finance + 引号公司名时易返回无关 PDF；POC 默认 7 日用 ``month`` 更稳
    if d <= 31:
        return "month"
    return "year"


def search_news(
    query: str,
    days_back: int = 7,
    max_results: int = 10,
    *,
    include_domains: list[str] | None = None,
    topic: str = "finance",
) -> list[dict[str, Any]]:
    """
    调用 Tavily ``POST https://api.tavily.com/search``。

    ``days_back`` 映射为 Tavily 的 ``time_range``（``day`` / ``week`` / ``month`` / ``year``）；
    经测 ``start_date``+``end_date`` 与 ``topic=news`` 组合易过窄导致无命中，故不发送
    用户描述的 ``{"days": ...}`` 对象。

    每条返回字典至少含：``title``, ``url``, ``content``；``published_date`` 若响应中有则保留，否则 ``None``。
    """
    key = _api_key()
    q = (query or "").strip()
    if not key or not q:
        if not key:
            logger.debug("Tavily：未配置 TAVILY_API_KEY，跳过搜索")
        return []

    n = max(1, min(int(max_results), 20))
    domains = include_domains if include_domains is not None else _DEFAULT_INCLUDE_DOMAINS

    payload: dict[str, Any] = {
        "api_key": key,
        "query": q,
        "search_depth": "advanced",
        "include_domains": domains,
        "max_results": n,
        "time_range": _time_range_for_days(days_back),
        "topic": topic if topic in ("general", "news", "finance") else "finance",
    }

    try:
        r = requests.post(
            TAVILY_SEARCH_URL,
            json=payload,
            headers={"Content-Type": "application/json"},
            timeout=_DEFAULT_TIMEOUT_SEC,
        )
        if r.status_code == 401:
            logger.warning("Tavily 401：API Key 无效或未传")
            return []
        if r.status_code == 429:
            logger.warning("Tavily 429：请求过于频繁")
            return []
        r.raise_for_status()
        data = r.json()
    except requests.Timeout:
        logger.warning("Tavily 请求超时 query=%r", q[:120])
        return []
    except requests.RequestException as e:
        logger.warning("Tavily 请求失败: %s", e)
        return []
    except ValueError as e:
        logger.warning("Tavily 响应非 JSON: %s", e)
        return []

    if not isinstance(data, dict):
        return []

    err = data.get("detail")
    if isinstance(err, dict) and err.get("error"):
        logger.warning("Tavily 错误: %s", err.get("error"))
        return []

    raw_results = data.get("results")
    if not isinstance(raw_results, list):
        return []

    out: list[dict[str, Any]] = []
    for item in raw_results:
        if not isinstance(item, dict):
            continue
        title = str(item.get("title") or "").strip()
        url = str(item.get("url") or "").strip()
        content = str(item.get("content") or "").strip()
        pub = item.get("published_date")
        if pub is None:
            pub = item.get("publishedDate") or item.get("published_time")
        pub_str = str(pub).strip()[:32] if pub is not None else ""

        out.append(
            {
                "title": title,
                "url": url,
                "content": content,
                "published_date": pub_str or None,
            }
        )
    return out
