"""隔夜速递时间窗与 Finnhub Unix 优先过滤的单元测试。"""
from __future__ import annotations

import os
import unittest
from datetime import datetime, timezone
from zoneinfo import ZoneInfo

from research_automation.core.news_time import (
    article_published_in_news_tz,
    filter_articles_in_half_open_window,
    overnight_window,
)
from research_automation.extractors.news_client import RawArticle

_NY = ZoneInfo("America/New_York")


def _utc_iso(dt_utc: datetime) -> str:
    return dt_utc.replace(microsecond=0).isoformat().replace("+00:00", "Z")


def _article_rss_only(ts_ny: datetime) -> RawArticle:
    """仅 RSS 时间（无 Finnhub Unix），用于回退路径。"""
    dt_utc = ts_ny.astimezone(timezone.utc)
    return {
        "title": "rss",
        "link": "https://example.com/rss",
        "description": "",
        "source": "Reuters",
        "published_at_utc": _utc_iso(dt_utc),
    }


def _article_finnhub_priority(ts_ny: datetime, *, misleading_rss_ny: datetime) -> RawArticle:
    """
    同时带 Finnhub Unix 与 ``published_at_utc``：过滤与展示应以 Unix 对应时刻为准。

    ``misleading_rss_ny`` 故意与 ``ts_ny`` 不同，用于断言优先逻辑。
    """
    dt_utc_real = ts_ny.astimezone(timezone.utc)
    dt_utc_fake = misleading_rss_ny.astimezone(timezone.utc)
    return {
        "title": "fh",
        "link": "https://example.com/fh",
        "description": "",
        "source": "Finnhub-API",
        "published_at_utc": _utc_iso(dt_utc_fake),
        "finnhub_datetime_unix": int(ts_ny.timestamp()),
    }


class TestOvernightWindow(unittest.TestCase):
    """模拟不同「当前时刻」下隔夜半开区间边界。"""

    def setUp(self) -> None:
        self._prev = os.environ.get("NEWS_TIMEZONE")
        os.environ["NEWS_TIMEZONE"] = "America/New_York"

    def tearDown(self) -> None:
        if self._prev is None:
            os.environ.pop("NEWS_TIMEZONE", None)
        else:
            os.environ["NEWS_TIMEZONE"] = self._prev

    def test_overnight_bounds(self) -> None:
        """纽约 6/15 07:00 时，窗口为 6/14 16:00 至 6/15 08:00（左闭右开）。"""
        now = datetime(2024, 6, 15, 7, 0, tzinfo=_NY)
        start, end = overnight_window(now_local=now)
        self.assertEqual(start, datetime(2024, 6, 14, 16, 0, tzinfo=_NY))
        self.assertEqual(end, datetime(2024, 6, 15, 8, 0, tzinfo=_NY))

    def test_filter_includes_start_excludes_end(self) -> None:
        """边界：起点纳入，终点不纳入。"""
        now = datetime(2024, 6, 15, 7, 0, tzinfo=_NY)
        start, end = overnight_window(now_local=now)
        at_start = _article_rss_only(datetime(2024, 6, 14, 16, 0, tzinfo=_NY))
        at_end = _article_rss_only(datetime(2024, 6, 15, 8, 0, tzinfo=_NY))
        before = _article_rss_only(datetime(2024, 6, 14, 15, 59, tzinfo=_NY))
        got = filter_articles_in_half_open_window([at_start, at_end, before], start, end)
        titles = {a["title"] for a in got}
        self.assertEqual(titles, {"rss"})
        self.assertEqual(len(got), 1)
        self.assertEqual(got[0]["title"], "rss")
        self.assertEqual(
            article_published_in_news_tz(at_start),
            datetime(2024, 6, 14, 16, 0, tzinfo=_NY),
        )

    def test_finnhub_unix_overrides_misleading_rss_in_window(self) -> None:
        """Finnhub Unix 在窗内、RSS 时间被故意设在窗外时：应保留。"""
        now = datetime(2024, 6, 15, 7, 0, tzinfo=_NY)
        start, end = overnight_window(now_local=now)
        real = datetime(2024, 6, 14, 20, 0, tzinfo=_NY)
        fake = datetime(2024, 6, 10, 12, 0, tzinfo=_NY)
        a = _article_finnhub_priority(real, misleading_rss_ny=fake)
        got = filter_articles_in_half_open_window([a], start, end)
        self.assertEqual(len(got), 1)
        self.assertEqual(article_published_in_news_tz(a), real.replace(microsecond=0))

    def test_finnhub_unix_outside_window_excludes_even_if_rss_inside(self) -> None:
        """Finnhub Unix 在窗外、RSS 时间在窗内：以 Finnhub 为准，应剔除。"""
        now = datetime(2024, 6, 15, 7, 0, tzinfo=_NY)
        start, end = overnight_window(now_local=now)
        real = datetime(2024, 6, 13, 12, 0, tzinfo=_NY)
        fake = datetime(2024, 6, 15, 6, 0, tzinfo=_NY)
        a = _article_finnhub_priority(real, misleading_rss_ny=fake)
        got = filter_articles_in_half_open_window([a], start, end)
        self.assertEqual(got, [])

    def test_no_finnhub_rss_fallback(self) -> None:
        """无 Finnhub 字段时，仅依赖 ``published_at_utc`` 仍可命中窗口。"""
        now = datetime(2024, 6, 15, 7, 0, tzinfo=_NY)
        start, end = overnight_window(now_local=now)
        a = _article_rss_only(datetime(2024, 6, 15, 6, 30, tzinfo=_NY))
        got = filter_articles_in_half_open_window([a], start, end)
        self.assertEqual(len(got), 1)


if __name__ == "__main__":
    unittest.main()
