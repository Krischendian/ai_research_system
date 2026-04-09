"""
Finnhub 公司 / 宏观新闻 API（免费层须限速与缓存，见 README）。

- 公司新闻：``/api/v1/company-news``
- 宏观（可选）：``/api/v1/news?category=general``
"""
from __future__ import annotations

import json
import logging
import os
import time
from datetime import date, datetime, timezone
from pathlib import Path
from typing import Any, TypedDict

from typing_extensions import NotRequired
from zoneinfo import ZoneInfo

import requests
from dotenv import load_dotenv

from research_automation.extractors.news_client import RawArticle

logger = logging.getLogger(__name__)

# 与 llm_client 一致：导入时加载项目根 .env，便于本地脚本未 export 也能读到 FINNHUB_API_KEY
load_dotenv(Path(__file__).resolve().parents[3] / ".env", override=False)

_NY = ZoneInfo("America/New_York")
_CACHE_TTL_SEC = 24 * 3600
_FINNHUB_GAP_SEC = 1.0  # 免费层约 60 次/分钟，保守间隔 1 秒
_LAST_REQ_MONO: float = 0.0

# 宏观新闻单次拉取条数上限（可选接口）
_MACRO_MAX_ITEMS = 40


def _project_root() -> Path:
    return Path(__file__).resolve().parents[3]


def _cache_dir() -> Path:
    d = _project_root() / "data" / "raw" / "finnhub"
    d.mkdir(parents=True, exist_ok=True)
    return d


def _finnhub_token() -> str | None:
    t = (os.environ.get("FINNHUB_API_KEY") or "").strip()
    return t or None


def _throttle_before_request() -> None:
    """两次实际 HTTP 请求之间至少间隔 ``_FINNHUB_GAP_SEC``（命中磁盘缓存时不调用）。"""
    global _LAST_REQ_MONO

    now = time.monotonic()
    wait = _FINNHUB_GAP_SEC - (now - _LAST_REQ_MONO)
    if wait > 0:
        time.sleep(wait)
    _LAST_REQ_MONO = time.monotonic()


def _normalize_date(d: date | str) -> str:
    if isinstance(d, date):
        return d.isoformat()
    s = str(d).strip()
    return s[:10] if len(s) >= 10 else s


class CompanyNewsItem(TypedDict):
    """单条公司新闻（面向调用方）。"""

    title: str
    summary: str
    url: str
    source: str
    # America/New_York 本地时间 ISO8601
    datetime: str
    # Finnhub 原始 Unix 秒（用于时间窗优先于 RSS/派生 ISO）
    datetime_unix: NotRequired[int]


def _cache_path_company(symbol: str, from_d: str, to_d: str) -> Path:
    sym = (symbol or "").strip().upper()
    return _cache_dir() / f"company_{sym}_{from_d}_{to_d}.json"


def _read_cache_if_fresh(path: Path) -> Any | None:
    if not path.exists() or path.stat().st_size == 0:
        return None
    age = time.time() - path.stat().st_mtime
    if age > _CACHE_TTL_SEC:
        return None
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None


def _write_cache(path: Path, payload: Any) -> None:
    try:
        path.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")
    except OSError as e:
        logger.warning("Finnhub 缓存写入失败 %s: %s", path, e)


def _api_item_to_company_news_item(raw: dict[str, Any]) -> CompanyNewsItem | None:
    """将 Finnhub 单条 JSON 转为结构化条目；``datetime`` 为纽约本地 ISO，``datetime_unix`` 为 API 原始 Unix 秒。"""
    headline = (raw.get("headline") or raw.get("title") or "").strip()
    if not headline:
        return None
    ts = raw.get("datetime")
    try:
        sec = int(ts)
    except (TypeError, ValueError):
        logger.debug("Finnhub 条目缺有效 datetime，跳过: %s", raw)
        return None
    dt_utc = datetime.fromtimestamp(sec, tz=timezone.utc)
    dt_ny = dt_utc.astimezone(_NY).replace(microsecond=0)
    src = (raw.get("source") or "Finnhub").strip() or "Finnhub"
    return CompanyNewsItem(
        title=headline,
        summary=(raw.get("summary") or "").strip(),
        url=(raw.get("url") or "").strip(),
        source=src,
        datetime=dt_ny.isoformat(),
        datetime_unix=sec,
    )


def get_company_news(
    ticker: str,
    from_date: date | str,
    to_date: date | str,
) -> list[CompanyNewsItem]:
    """
    拉取 Finnhub 公司新闻。

    :return: 每条含 title, summary, url, source, datetime（纽约时区 ISO 字符串）。
    """
    sym = (ticker or "").strip().upper()
    if not sym:
        logger.warning("Finnhub：ticker 为空")
        return []

    token = _finnhub_token()
    if not token:
        logger.warning("Finnhub：未设置环境变量 FINNHUB_API_KEY，跳过公司新闻")
        return []

    fd = _normalize_date(from_date)
    td = _normalize_date(to_date)
    cpath = _cache_path_company(sym, fd, td)
    cached = _read_cache_if_fresh(cpath)
    if isinstance(cached, list):
        out: list[CompanyNewsItem] = []
        for it in cached:
            if isinstance(it, dict) and all(
                k in it for k in ("title", "summary", "url", "source", "datetime")
            ):
                row = CompanyNewsItem(
                    title=str(it["title"]),
                    summary=str(it["summary"]),
                    url=str(it["url"]),
                    source=str(it["source"]),
                    datetime=str(it["datetime"]),
                )
                if "datetime_unix" in it:
                    try:
                        row["datetime_unix"] = int(it["datetime_unix"])
                    except (TypeError, ValueError):
                        pass
                out.append(row)
        return out

    url = "https://finnhub.io/api/v1/company-news"
    params = {"symbol": sym, "from": fd, "to": td, "token": token}
    _throttle_before_request()
    try:
        resp = requests.get(url, params=params, timeout=30)
        resp.raise_for_status()
        data = resp.json()
    except (requests.RequestException, ValueError) as e:
        logger.warning("Finnhub company-news 请求失败 symbol=%s: %s", sym, e)
        return []

    if not isinstance(data, list):
        logger.warning("Finnhub company-news 返回非列表 symbol=%s", sym)
        return []

    items: list[CompanyNewsItem] = []
    for row in data:
        if not isinstance(row, dict):
            continue
        it = _api_item_to_company_news_item(row)
        if it is not None:
            items.append(it)

    _write_cache(cpath, list(items))
    return items


def company_news_item_to_raw_article(item: CompanyNewsItem, symbol: str) -> RawArticle:
    """转为与 RSS 统一的 ``RawArticle``；若存在 ``datetime_unix`` 则写入 ``finnhub_datetime_unix`` 供时间窗优先使用。"""
    dt_ny = datetime.fromisoformat(item["datetime"])
    if dt_ny.tzinfo is None:
        dt_ny = dt_ny.replace(tzinfo=_NY)
    utc = dt_ny.astimezone(timezone.utc).replace(microsecond=0)
    pub = utc.isoformat().replace("+00:00", "Z")
    src = item["source"]
    label = f"Finnhub-{src}" if src else "Finnhub"
    raw: RawArticle = RawArticle(
        title=item["title"],
        link=item["url"],
        description=item["summary"],
        source=label,
        published_at_utc=pub,
        implied_tickers=[symbol.strip().upper()],
    )
    du = item.get("datetime_unix")
    if du is not None:
        try:
            raw["finnhub_datetime_unix"] = int(du)
        except (TypeError, ValueError):
            pass
    return raw


def fetch_finnhub_raw_articles_for_tickers(
    tickers: list[str],
    from_date: date | str,
    to_date: date | str,
) -> list[RawArticle]:
    """
    对多个 ticker 依次请求公司新闻并转为 ``RawArticle``（内部已限速；有 24h 文件缓存）。
    """
    out: list[RawArticle] = []
    for sym in tickers:
        sym = sym.strip().upper()
        if not sym:
            continue
        for it in get_company_news(sym, from_date, to_date):
            out.append(company_news_item_to_raw_article(it, sym))
    return out


def merge_finnhub_and_rss(
    finnhub: list[RawArticle],
    rss: list[RawArticle],
) -> list[RawArticle]:
    """
    合并 Finnhub 与 RSS 列表，按 **url（小写）** 与 **title（小写）** 去重；
    Finnhub 条目在前，重复项保留先出现者。
    """
    seen_url: set[str] = set()
    seen_title: set[str] = set()
    merged: list[RawArticle] = []
    for a in finnhub + rss:
        url = (a.get("link") or "").strip().lower()
        title = (a.get("title") or "").strip().lower()
        if not title:
            continue
        if url and url in seen_url:
            continue
        if title in seen_title:
            continue
        if url:
            seen_url.add(url)
        seen_title.add(title)
        merged.append(a)
    return merged


def _cache_path_macro() -> Path:
    return _cache_dir() / "macro_general.json"


def get_macro_news() -> list[CompanyNewsItem]:
    """
    （可选）Finnhub 全市场 / 宏观类新闻 ``category=general``。

    若未配置 API Key 或请求失败，返回空列表。结果带 24h 缓存。
    """
    token = _finnhub_token()
    if not token:
        logger.debug("Finnhub：无 API Key，跳过 get_macro_news")
        return []

    cpath = _cache_path_macro()
    cached = _read_cache_if_fresh(cpath)
    if isinstance(cached, list):
        out: list[CompanyNewsItem] = []
        for it in cached[:_MACRO_MAX_ITEMS]:
            if isinstance(it, dict) and all(
                k in it for k in ("title", "summary", "url", "source", "datetime")
            ):
                row = CompanyNewsItem(
                    title=str(it["title"]),
                    summary=str(it["summary"]),
                    url=str(it["url"]),
                    source=str(it["source"]),
                    datetime=str(it["datetime"]),
                )
                if "datetime_unix" in it:
                    try:
                        row["datetime_unix"] = int(it["datetime_unix"])
                    except (TypeError, ValueError):
                        pass
                out.append(row)
        return out

    url = "https://finnhub.io/api/v1/news"
    params = {"category": "general", "token": token}
    _throttle_before_request()
    try:
        resp = requests.get(url, params=params, timeout=30)
        resp.raise_for_status()
        data = resp.json()
    except (requests.RequestException, ValueError) as e:
        logger.warning("Finnhub macro news 请求失败: %s", e)
        return []

    if not isinstance(data, list):
        return []

    items: list[CompanyNewsItem] = []
    for row in data[:_MACRO_MAX_ITEMS]:
        if not isinstance(row, dict):
            continue
        it = _api_item_to_company_news_item(row)
        if it is not None:
            items.append(it)

    _write_cache(cpath, list(items))
    return items
