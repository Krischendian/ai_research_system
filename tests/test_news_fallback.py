"""晨报辅助：Finnhub/RSS 区分与公司条目排序。"""
from __future__ import annotations

import unittest
from unittest.mock import patch

from research_automation.extractors.news_client import RawArticle
from research_automation.services import news_service


class TestNewsHelpers(unittest.TestCase):
    def test_is_finnhub_article(self) -> None:
        """来源以 Finnhub / Benzinga 前缀为判据，用于合并列表分区。"""
        fh: RawArticle = {
            "title": "t",
            "link": "u",
            "description": "",
            "source": "Finnhub-Bloomberg",
        }
        bz: RawArticle = {
            "title": "t1",
            "link": "u1",
            "description": "",
            "source": "Benzinga-Newsdesk",
        }
        rss: RawArticle = {
            "title": "t2",
            "link": "u2",
            "description": "",
            "source": "Reuters",
        }
        self.assertTrue(news_service._is_finnhub_article(fh))
        self.assertTrue(news_service._is_finnhub_article(bz))
        self.assertFalse(news_service._is_finnhub_article(rss))

    @patch("research_automation.services.news_service.extract_tickers_from_text")
    def test_prioritize_puts_hits_first(self, mock_xt) -> None:
        """已命中 ticker 的 Finnhub 条目排在未命中之前。"""
        mock_xt.side_effect = lambda blob: (
            ["MSFT"] if "Microsoft" in blob else (["AAPL"] if "Apple" in blob else [])
        )
        arts: list[RawArticle] = [
            {
                "title": "Oil war",
                "link": "",
                "description": "energy",
                "source": "Finnhub-X",
            },
            {
                "title": "Microsoft AI",
                "link": "",
                "description": "cloud",
                "source": "Finnhub-X",
            },
            {
                "title": "Apple store",
                "link": "",
                "description": "retail",
                "source": "Finnhub-X",
            },
        ]
        out = news_service._prioritize_articles_with_tickers(arts)
        titles = [x["title"] for x in out]
        self.assertLess(titles.index("Microsoft AI"), titles.index("Oil war"))
        self.assertLess(titles.index("Apple store"), titles.index("Oil war"))


if __name__ == "__main__":
    unittest.main()
