"""财报电话会：Mock 逐字稿与 LLM 解析。"""
from __future__ import annotations

import json
import unittest
from unittest.mock import patch

from research_automation.extractors.earnings_call import get_transcript
from research_automation.services.earnings_service import analyze_earnings_call


class TestEarningsMock(unittest.TestCase):
    def test_get_transcript_mock_non_empty(self) -> None:
        t = get_transcript("AAPL", 2024, 4)
        self.assertIn("MOCK TRANSCRIPT", t)
        self.assertIn("Tim Cook", t)

    @patch("research_automation.services.earnings_service.chat")
    def test_analyze_parses_llm_json(self, mock_chat) -> None:
        mock_chat.return_value = json.dumps(
            {
                "summary": "本季度营收稳健，服务创新高。",
                "management_viewpoints": ["iPhone 表现良好", "服务双位数增长"],
                "quotations": [
                    {
                        "speaker": "Tim Cook",
                        "quote": "We are pleased with record revenue for the quarter, with strength",
                        "topic": "Results",
                    }
                ],
                "new_business_highlights": ["加大生成式 AI 投入"],
            },
            ensure_ascii=False,
        )
        out = analyze_earnings_call("MSFT", 2024, 4)
        self.assertEqual(out.ticker, "MSFT")
        self.assertEqual(out.quarter, "2024Q4")
        self.assertIn("营收", out.summary)
        self.assertGreaterEqual(len(out.management_viewpoints), 1)
        self.assertTrue(out.management_viewpoints[0].text)
        self.assertGreaterEqual(len(out.quotations), 1)
        self.assertIsInstance(out.document_uid, str)
        mock_chat.assert_called_once()


if __name__ == "__main__":
    unittest.main()
