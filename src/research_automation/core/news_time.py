"""新闻时间窗与发布时间解析：优先 Finnhub Unix 时间戳，缺省则回退 RSS 的 UTC ISO。"""
from __future__ import annotations

import os
from datetime import datetime, timedelta, time, timezone
from pathlib import Path
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from dotenv import load_dotenv

from research_automation.extractors.news_client import RawArticle, parse_published_at_utc

# 与 extractors 一致：保证未 export 时也能读到项目根 .env 中的 NEWS_TIMEZONE
load_dotenv(Path(__file__).resolve().parents[3] / ".env", override=False)


def get_news_zoneinfo() -> ZoneInfo:
    """
    从环境变量 ``NEWS_TIMEZONE`` 读取 IANA 时区名，解析为 ``ZoneInfo``。

    未设置时默认为 ``America/New_York``；名称非法时回退至纽约，避免服务崩溃。
    """
    name = (os.environ.get("NEWS_TIMEZONE") or "America/New_York").strip()
    if not name:
        name = "America/New_York"
    try:
        return ZoneInfo(name)
    except (ZoneInfoNotFoundError, ValueError):
        return ZoneInfo("America/New_York")


def get_news_timezone_name() -> str:
    """返回当前生效的新闻日历时区名（与 ``get_news_zoneinfo().key`` 一致，便于溯源文案）。"""
    return get_news_zoneinfo().key


def article_published_in_news_tz(article: RawArticle) -> datetime | None:
    """
    将单条新闻映射到「新闻日历时区」下的本地时刻，用于时间窗过滤与排序。

    优先使用 Finnhub 返回的 Unix 秒级时间戳（``finnhub_datetime_unix``），
    与 ``published_at_utc`` 不一致时以 Finnhub 为准；若无 Unix 字段则解析 RSS 的 UTC ISO。
    无法解析时返回 ``None``（该条目不参与按时间的窗内筛选）。
    """
    tz = get_news_zoneinfo()
    uni = article.get("finnhub_datetime_unix")
    if uni is not None:
        try:
            sec = int(uni)
            dt_utc = datetime.fromtimestamp(sec, tz=timezone.utc)
            return dt_utc.astimezone(tz).replace(microsecond=0)
        except (TypeError, ValueError, OSError):
            pass
    iso = article.get("published_at_utc")
    if not iso:
        return None
    try:
        return parse_published_at_utc(str(iso)).astimezone(tz).replace(microsecond=0)
    except (TypeError, ValueError):
        return None


def article_sort_instant_utc(article: RawArticle) -> datetime:
    """
    用于排序的 UTC 时刻：优先 Finnhub Unix，否则 ``published_at_utc``；

    皆不可用时返回 UTC 最小值，保证稳定排在末尾。
    """
    uni = article.get("finnhub_datetime_unix")
    if uni is not None:
        try:
            return datetime.fromtimestamp(int(uni), tz=timezone.utc)
        except (TypeError, ValueError, OSError):
            pass
    p = article.get("published_at_utc")
    if p:
        try:
            return parse_published_at_utc(str(p))
        except (TypeError, ValueError):
            pass
    return datetime.min.replace(tzinfo=timezone.utc)


def overnight_window(
    *,
    now_local: datetime | None = None,
) -> tuple[datetime, datetime]:
    """
    新闻日历下的隔夜半开区间：「昨天 16:00」至「今天 08:00``[start, end)``。

    ``now_local`` 仅用于测试注入；默认取 ``datetime.now(新闻时区)``。
    若 ``now_local`` 无时区信息，则视为已在新闻时区本地墙上时间。
    """
    tz = get_news_zoneinfo()
    now = now_local or datetime.now(tz)
    if now.tzinfo is None:
        now = now.replace(tzinfo=tz)
    else:
        now = now.astimezone(tz)
    d = now.date()
    start = datetime.combine(d - timedelta(days=1), time(16, 0), tzinfo=tz)
    end = datetime.combine(d, time(8, 0), tzinfo=tz)
    return start, end


def yesterday_full_day_window(
    *,
    now_local: datetime | None = None,
) -> tuple[datetime, datetime]:
    """
    新闻日历「昨日」全天：昨日 00:00 至今日 00:00，半开区间 ``[start, end)``。

    与「昨日 00:00–23:59:59」在常用精度下等价；边界为今日 0 点整不包含。
    """
    tz = get_news_zoneinfo()
    now = now_local or datetime.now(tz)
    if now.tzinfo is None:
        now = now.replace(tzinfo=tz)
    else:
        now = now.astimezone(tz)
    today_d = now.date()
    yday_d = today_d - timedelta(days=1)
    start = datetime.combine(yday_d, time(0, 0), tzinfo=tz)
    end = datetime.combine(today_d, time(0, 0), tzinfo=tz)
    return start, end


def filter_articles_in_half_open_window(
    articles: list[RawArticle],
    start: datetime,
    end: datetime,
) -> list[RawArticle]:
    """
    保留发布时刻落在 ``[start, end)``（与 start/end 同时区）内的条目。

    发布时刻由 ``article_published_in_news_tz`` 决定（Finnhub 优先）；无有效时间则丢弃。
    结果按时间从新到旧排序（UTC 瞬间比较）。
    """
    out: list[RawArticle] = []
    for a in articles:
        pub = article_published_in_news_tz(a)
        if pub is None:
            continue
        if start <= pub < end:
            out.append(a)
    out.sort(key=article_sort_instant_utc, reverse=True)
    return out
