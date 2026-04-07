"""新闻 ticker 关键词匹配。"""
from __future__ import annotations

import unittest
from unittest.mock import patch

from research_automation.core.company_manager import CompanyRecord
from research_automation.extractors.news_client import extract_tickers_from_text


class TestExtractTickers(unittest.TestCase):
    @patch("research_automation.core.company_manager.list_companies")
    def test_cashtag_and_name(self, mock_lc) -> None:
        mock_lc.return_value = [
            CompanyRecord("AAPL", "Apple Inc.", "Technology", "US", 1),
            CompanyRecord("MSFT", "Microsoft Corporation", "Technology", "US", 1),
        ]
        t = extract_tickers_from_text("Shares of $AAPL rose after Apple Inc. guided higher.")
        self.assertIn("AAPL", t)
        t2 = extract_tickers_from_text("MSFT cloud revenue and Microsoft Corporation outlook.")
        self.assertIn("MSFT", t2)

    @patch("research_automation.core.company_manager.list_companies")
    def test_word_boundary_meta(self, mock_lc) -> None:
        mock_lc.return_value = [
            CompanyRecord("META", "Meta Platforms Inc.", "Technology", "US", 1),
        ]
        self.assertIn("META", extract_tickers_from_text("META stock volatility today"))

    @patch("research_automation.core.company_manager.list_companies")
    def test_chinese_summary_aliases(self, mock_lc) -> None:
        mock_lc.return_value = [
            CompanyRecord("AAPL", "Apple Inc.", "Technology", "US", 1),
        ]
        zh = "苹果公司发布新品，市场预期收入将增长。"
        self.assertIn("AAPL", extract_tickers_from_text(zh))


if __name__ == "__main__":
    unittest.main()
