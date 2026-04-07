"""晨报：RSS 直配兜底与排序。"""
from __future__ import annotations

import unittest
from unittest.mock import patch

from research_automation.extractors.news_client import RawArticle
from research_automation.services import news_service


class TestNewsFallback(unittest.TestCase):
    @patch("research_automation.services.news_service.extract_tickers_from_text")
    def test_fallback_builds_items_when_tickers_hit(self, mock_xt) -> None:
        def _xt(blob: str) -> list[str]:
            return ["AAPL"] if "Apple" in blob else []

        mock_xt.side_effect = _xt
        arts: list[RawArticle] = [
            RawArticle(
                title="Apple previews new software",
                link="https://example.com/a",
                description="The company said developers will get tools.",
                source="TechCrunch",
            ),
            RawArticle(
                title="Oil prices mixed",
                link="https://example.com/o",
                description="Crude futures.",
                source="Bloomberg",
            ),
        ]
        items = news_service._fallback_company_from_rss(arts, limit=5)
        self.assertEqual(len(items), 1)
        self.assertEqual(items[0].title, "Apple previews new software")
        self.assertIn("AAPL", items[0].matched_tickers)

    @patch("research_automation.services.news_service.extract_tickers_from_text")
    def test_prioritize_puts_hits_first(self, mock_xt) -> None:
        mock_xt.side_effect = lambda blob: (
            ["MSFT"] if "Microsoft" in blob else (["AAPL"] if "Apple" in blob else [])
        )
        arts: list[RawArticle] = [
            RawArticle(
                title="Oil war",
                link="",
                description="energy",
                source="B",
            ),
            RawArticle(
                title="Microsoft AI",
                link="",
                description="cloud",
                source="B",
            ),
            RawArticle(
                title="Apple store",
                link="",
                description="retail",
                source="B",
            ),
        ]
        out = news_service._prioritize_articles_with_tickers(arts)
        titles = [x["title"] for x in out]
        self.assertLess(titles.index("Microsoft AI"), titles.index("Oil war"))
        self.assertLess(titles.index("Apple store"), titles.index("Oil war"))


if __name__ == "__main__":
    unittest.main()
