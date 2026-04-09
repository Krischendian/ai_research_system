"""
Benzinga News API v2（公司新闻结构化流）。

文档：https://docs.benzinga.com/api-reference/news-api/get-news-items
"""
from __future__ import annotations

import json
import logging
import os
import time
from datetime import datetime, timezone
from email.utils import parsedate_to_datetime
from pathlib import Path
from typing import Any, TypedDict

import requests
from dotenv import load_dotenv

from research_automation.extractors.news_client import RawArticle

logger = logging.getLogger(__name__)

load_dotenv(Path(__file__).resolve().parents[3] / ".env", override=False)

BASE_URL = "https://api.benzinga.com/api/v2"
_CACHE_TTL_SEC = 24 * 3600
_MAX_PAGES = 25
_PAGE_SIZE = 100


class BenzingaNewsDict(TypedDict):
    """单条新闻（调用方可见字段）。"""

    title: str
    summary: str
    url: str
    source: str
    published_at: str


def _project_root() -> Path:
    return Path(__file__).resolve().parents[3]


def _cache_dir() -> Path:
    d = _project_root() / "data" / "raw" / "benzinga"
    d.mkdir(parents=True, exist_ok=True)
    return d


def _api_key() -> str | None:
    t = (os.environ.get("BENZINGA_API_KEY") or "").strip()
    return t or None


def _cache_path(symbol: str, from_d: str, to_d: str) -> Path:
    sym = (symbol or "").strip().upper()
    return _cache_dir() / f"company_{sym}_{from_d}_{to_d}.json"


def _read_cache_if_fresh(path: Path) -> list[BenzingaNewsDict] | None:
    if not path.exists() or path.stat().st_size == 0:
        return None
    if time.time() - path.stat().st_mtime > _CACHE_TTL_SEC:
        return None
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None
    if not isinstance(data, list):
        return None
    out: list[BenzingaNewsDict] = []
    for it in data:
        if isinstance(it, dict) and all(
            k in it for k in ("title", "summary", "url", "source", "published_at")
        ):
            out.append(
                BenzingaNewsDict(
                    title=str(it["title"]),
                    summary=str(it["summary"]),
                    url=str(it["url"]),
                    source=str(it["source"]),
                    published_at=str(it["published_at"]),
                )
            )
    return out


def _write_cache(path: Path, payload: list[BenzingaNewsDict]) -> None:
    try:
        path.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")
    except OSError as e:
        logger.warning("Benzinga 缓存写入失败 %s: %s", path, e)


def _normalize_date(s: str) -> str:
    t = (s or "").strip()
    return t[:10] if len(t) >= 10 else t


def _created_to_published_iso(created: str) -> str | None:
    s = (created or "").strip()
    if not s:
        return None
    try:
        dt = parsedate_to_datetime(s)
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        utc = dt.astimezone(timezone.utc).replace(microsecond=0)
        return utc.isoformat().replace("+00:00", "Z")
    except (TypeError, ValueError):
        return None


def _row_to_item(row: dict[str, Any]) -> BenzingaNewsDict | None:
    title = (row.get("title") or "").strip()
    if not title:
        return None
    created = row.get("created") or row.get("updated") or ""
    pub = _created_to_published_iso(str(created))
    if not pub:
        logger.debug("Benzinga 条目缺可解析时间，跳过: %s", row.get("id"))
        return None
    teaser = (row.get("teaser") or "").strip()
    body = (row.get("body") or "").strip()
    summary = teaser or body
    url = (row.get("url") or "").strip()
    author = (row.get("author") or "").strip()
    source = f"Benzinga-{author}" if author else "Benzinga"
    return BenzingaNewsDict(
        title=title,
        summary=summary,
        url=url,
        source=source,
        published_at=pub,
    )


def get_company_news(ticker: str, from_date: str, to_date: str) -> list[BenzingaNewsDict]:
    """
    获取指定 ticker 在日期范围内的新闻。

    :return: 每条含 title, summary, url, source, published_at（UTC ISO8601，Z 结尾）。
    """
    sym = (ticker or "").strip().upper()
    if not sym:
        logger.warning("Benzinga：ticker 为空")
        return []

    key = _api_key()
    if not key:
        logger.warning("Benzinga：未设置 BENZINGA_API_KEY，跳过")
        return []

    fd = _normalize_date(from_date)
    td = _normalize_date(to_date)
    if not fd or not td:
        return []

    cpath = _cache_path(sym, fd, td)
    cached = _read_cache_if_fresh(cpath)
    if cached is not None:
        return cached

    url = f"{BASE_URL}/news"
    headers = {"Accept": "application/json"}
    collected: list[BenzingaNewsDict] = []
    page = 0
    while page < _MAX_PAGES:
        params: dict[str, str | int] = {
            "token": key,
            "tickers": sym,
            "dateFrom": fd,
            "dateTo": td,
            "pageSize": _PAGE_SIZE,
            "page": page,
            "displayOutput": "abstract",
        }
        try:
            resp = requests.get(url, params=params, headers=headers, timeout=45)
            resp.raise_for_status()
            data = resp.json()
        except (requests.RequestException, ValueError) as e:
            logger.warning("Benzinga news 请求失败 symbol=%s page=%s: %s", sym, page, e)
            break

        if isinstance(data, dict) and data.get("ok") is False:
            logger.warning("Benzinga news 返回错误 symbol=%s: %s", sym, data.get("errors"))
            break

        if not isinstance(data, list):
            logger.warning("Benzinga news 返回非列表 symbol=%s", sym)
            break

        batch = 0
        for row in data:
            if not isinstance(row, dict):
                continue
            it = _row_to_item(row)
            if it is not None:
                collected.append(it)
                batch += 1

        if len(data) < _PAGE_SIZE:
            break
        if batch == 0:
            break
        page += 1

    _write_cache(cpath, collected)
    return collected


def benzinga_news_dict_to_raw_article(item: BenzingaNewsDict, symbol: str) -> RawArticle:
    """转为与 RSS / Finnhub 统一的 ``RawArticle``；Unix 时间写入 ``finnhub_datetime_unix`` 供时间窗逻辑复用。"""
    sym = symbol.strip().upper()
    try:
        iso = item["published_at"].replace("Z", "+00:00")
        dt = datetime.fromisoformat(iso)
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        unix = int(dt.timestamp())
    except (ValueError, TypeError, OSError):
        unix = None

    raw: RawArticle = RawArticle(
        title=item["title"],
        link=item["url"],
        description=item["summary"],
        source=item["source"],
        published_at_utc=item["published_at"],
        implied_tickers=[sym],
    )
    if unix is not None:
        raw["finnhub_datetime_unix"] = unix
    return raw
