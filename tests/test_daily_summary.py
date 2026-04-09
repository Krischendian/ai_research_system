"""昨日总结时间窗与 Finnhub Unix 优先过滤的单元测试。"""
from __future__ import annotations

import os
import unittest
from datetime import datetime, timezone
from zoneinfo import ZoneInfo

from research_automation.core.news_time import (
    article_published_in_news_tz,
    filter_articles_in_half_open_window,
    yesterday_full_day_window,
)
from research_automation.extractors.news_client import RawArticle

_NY = ZoneInfo("America/New_York")


def _utc_iso(dt_utc: datetime) -> str:
    return dt_utc.replace(microsecond=0).isoformat().replace("+00:00", "Z")


def _article_rss_only(ts_ny: datetime) -> RawArticle:
    """仅 RSS 时间，无 Finnhub Unix。"""
    dt_utc = ts_ny.astimezone(timezone.utc)
    return {
        "title": "rss2",
        "link": "https://example.com/r2",
        "description": "",
        "source": "Bloomberg",
        "published_at_utc": _utc_iso(dt_utc),
    }


def _article_finnhub_priority(ts_ny: datetime, *, misleading_rss_ny: datetime) -> RawArticle:
    """Finnhub Unix 与 RSS ISO 不一致时，以 Unix 为准。"""
    dt_utc_fake = misleading_rss_ny.astimezone(timezone.utc)
    return {
        "title": "fh2",
        "link": "https://example.com/fh2",
        "description": "",
        "source": "Finnhub-X",
        "published_at_utc": _utc_iso(dt_utc_fake),
        "finnhub_datetime_unix": int(ts_ny.timestamp()),
    }


class TestYesterdayWindow(unittest.TestCase):
    """昨日全天 [昨日0点, 今日0点) 与过滤逻辑。"""

    def setUp(self) -> None:
        self._prev = os.environ.get("NEWS_TIMEZONE")
        os.environ["NEWS_TIMEZONE"] = "America/New_York"

    def tearDown(self) -> None:
        if self._prev is None:
            os.environ.pop("NEWS_TIMEZONE", None)
        else:
            os.environ["NEWS_TIMEZONE"] = self._prev

    def test_yesterday_calendar(self) -> None:
        """当前为 6/15 10:00 纽约时，「昨日」为 6/14 整日半开区间。"""
        now = datetime(2024, 6, 15, 10, 0, tzinfo=_NY)
        start, end = yesterday_full_day_window(now_local=now)
        self.assertEqual(start, datetime(2024, 6, 14, 0, 0, tzinfo=_NY))
        self.assertEqual(end, datetime(2024, 6, 15, 0, 0, tzinfo=_NY))

    def test_yesterday_includes_last_minute(self) -> None:
        """昨日 23:59 纳入；今日 00:00 不纳入。"""
        now = datetime(2024, 6, 15, 10, 0, tzinfo=_NY)
        start, end = yesterday_full_day_window(now_local=now)
        late = _article_rss_only(datetime(2024, 6, 14, 23, 59, tzinfo=_NY))
        next_day_midnight = _article_rss_only(datetime(2024, 6, 15, 0, 0, tzinfo=_NY))
        got = filter_articles_in_half_open_window([late, next_day_midnight], start, end)
        self.assertEqual(len(got), 1)
        self.assertEqual(got[0]["title"], "rss2")

    def test_finnhub_priority_yesterday(self) -> None:
        """昨日窗口内：Finnhub 在昨日、RSS 误标为今日时仍计入昨日。"""
        now = datetime(2024, 6, 15, 10, 0, tzinfo=_NY)
        start, end = yesterday_full_day_window(now_local=now)
        real = datetime(2024, 6, 14, 18, 0, tzinfo=_NY)
        fake = datetime(2024, 6, 15, 9, 0, tzinfo=_NY)
        a = _article_finnhub_priority(real, misleading_rss_ny=fake)
        got = filter_articles_in_half_open_window([a], start, end)
        self.assertEqual(len(got), 1)
        self.assertEqual(article_published_in_news_tz(a), real.replace(microsecond=0))

    def test_finnhub_yesterday_excludes_prior_day_even_if_rss_says_yesterday(self) -> None:
        """真实 Unix 在前天：即使 RSS 写在「昨日」也不应入窗。"""
        now = datetime(2024, 6, 15, 10, 0, tzinfo=_NY)
        start, end = yesterday_full_day_window(now_local=now)
        real = datetime(2024, 6, 13, 12, 0, tzinfo=_NY)
        fake = datetime(2024, 6, 14, 10, 0, tzinfo=_NY)
        a = _article_finnhub_priority(real, misleading_rss_ny=fake)
        got = filter_articles_in_half_open_window([a], start, end)
        self.assertEqual(got, [])

    def test_empty_finnhub_rss_only_still_works(self) -> None:
        """无 Finnhub 数据时，仅用 RSS 时间戳仍可筛选。"""
        now = datetime(2024, 6, 15, 10, 0, tzinfo=_NY)
        start, end = yesterday_full_day_window(now_local=now)
        a = _article_rss_only(datetime(2024, 6, 14, 9, 30, tzinfo=_NY))
        got = filter_articles_in_half_open_window([a], start, end)
        self.assertEqual(len(got), 1)


if __name__ == "__main__":
    unittest.main()
