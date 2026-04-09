"""财报电话会：Mock 逐字稿与 LLM 解析。"""
from __future__ import annotations

import json
import unittest
from unittest.mock import patch

from research_automation.services.earnings_service import (
    EarningsAnalysisError,
    _quote_in_transcript,
    analyze_earnings_call,
)


class TestEarningsMock(unittest.TestCase):
    def test_quote_in_transcript_strips_wrapping_quotes(self) -> None:
        t = "Tim Cook: We are pleased with record revenue for the quarter."
        self.assertTrue(
            _quote_in_transcript(
                '"We are pleased with record revenue for the quarter."',
                t,
            )
        )

    def test_quote_in_transcript_fuzzy_whitespace_and_punct(self) -> None:
        t = (
            "Tim Cook:\n\nWe are pleased—with record revenue  for\n"
            "the quarter, and strength across iPhone."
        )
        self.assertTrue(
            _quote_in_transcript(
                "We are pleased with record revenue for the quarter",
                t,
            )
        )

    @patch("research_automation.services.earnings_service.chat")
    @patch(
        "research_automation.services.earnings_service.get_transcript_from_earningscall",
    )
    @patch(
        "research_automation.services.earnings_service.fmp_client.get_earnings_transcript",
        return_value=None,
    )
    @patch(
        "research_automation.services.earnings_service.search_8k_transcript",
        return_value=None,
    )
    @patch(
        "research_automation.services.earnings_service.fetch_transcript_from_8k",
        return_value=None,
    )
    def test_analyze_parses_llm_json(
        self,
        _mock_fetch,
        _mock_sec,
        _mock_fmp,
        mock_ec,
        mock_chat,
    ) -> None:
        # 固定一段含 LLM mock 原话的「伪逐字稿」，避免依赖外网 earningscall。
        mock_ec.return_value = (
            "Tim Cook — CEO: We are pleased with record revenue for the quarter, with strength\n"
            "across iPhone and Services.\n"
        )
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
        self.assertEqual(out.data_source, "earningscall")
        self.assertIn("营收", out.summary)
        self.assertGreaterEqual(len(out.management_viewpoints), 1)
        self.assertTrue(out.management_viewpoints[0].text)
        self.assertGreaterEqual(len(out.quotations), 1)
        self.assertIsInstance(out.document_uid, str)
        mock_chat.assert_called_once()

    @patch("research_automation.services.earnings_service.chat")
    @patch(
        "research_automation.services.earnings_service.get_transcript_from_earningscall",
    )
    @patch(
        "research_automation.services.earnings_service.fmp_client.get_earnings_transcript",
    )
    @patch(
        "research_automation.services.earnings_service.search_8k_transcript",
        return_value=None,
    )
    @patch(
        "research_automation.services.earnings_service.fetch_transcript_from_8k",
        return_value=None,
    )
    def test_analyze_uses_fmp_when_available(
        self,
        _mock_fetch,
        _mock_sec,
        mock_fmp,
        mock_ec,
        mock_chat,
    ) -> None:
        mock_fmp.return_value = {
            "quarter": "2024Q4",
            "date": "2024-10-01",
            "content": [
                {
                    "speaker": "Tim Cook",
                    "position": "CEO",
                    "text": "We are pleased with record revenue for the quarter.",
                },
            ],
        }
        mock_ec.return_value = "SHOULD NOT USE EC"
        mock_chat.return_value = json.dumps(
            {
                "summary": "本季度营收稳健。",
                "summary_source_paragraph_ids": [],
                "management_viewpoints": [{"text": "业绩良好", "source_paragraph_ids": []}],
                "quotations": [
                    {
                        "speaker": "Tim Cook",
                        "quote": "We are pleased with record revenue for the quarter.",
                        "topic": "Results",
                        "source_paragraph_ids": [],
                    }
                ],
                "new_business_highlights": [],
            },
            ensure_ascii=False,
        )
        out = analyze_earnings_call("MSFT", 2024, 4)
        self.assertEqual(out.data_source, "fmp")
        mock_ec.assert_not_called()
        mock_chat.assert_called_once()

    @patch("research_automation.services.earnings_service.chat")
    @patch(
        "research_automation.services.earnings_service.get_transcript_from_earningscall",
    )
    @patch(
        "research_automation.services.earnings_service.search_8k_transcript",
    )
    @patch(
        "research_automation.services.earnings_service.fmp_client.get_earnings_transcript",
        return_value=None,
    )
    @patch(
        "research_automation.services.earnings_service.fetch_transcript_from_8k",
        return_value=None,
    )
    def test_analyze_uses_sec_8k_when_fmp_empty(
        self,
        _mock_fetch,
        _mock_fmp,
        mock_sec,
        mock_ec,
        mock_chat,
    ) -> None:
        mock_sec.return_value = (
            "Operator: Good day. This is the fourth quarter 2024 earnings conference call. "
            "Forward-looking statements and safe harbor apply.\n\n"
            "Tim Cook: We are pleased with record revenue for the quarter."
        )
        mock_ec.return_value = "DO NOT USE EC"
        mock_chat.return_value = json.dumps(
            {
                "summary": "本季度营收稳健。",
                "summary_source_paragraph_ids": [],
                "management_viewpoints": [{"text": "业绩良好", "source_paragraph_ids": []}],
                "quotations": [
                    {
                        "speaker": "Tim Cook",
                        "quote": "We are pleased with record revenue for the quarter.",
                        "topic": "Results",
                        "source_paragraph_ids": [],
                    }
                ],
                "new_business_highlights": [],
            },
            ensure_ascii=False,
        )
        out = analyze_earnings_call("MSFT", 2024, 4)
        self.assertEqual(out.data_source, "sec_8k")
        mock_ec.assert_not_called()
        mock_chat.assert_called_once()

    @patch("research_automation.services.earnings_service.chat")
    @patch(
        "research_automation.services.earnings_service.get_transcript_from_earningscall",
        return_value="",
    )
    @patch(
        "research_automation.services.earnings_service.search_8k_transcript",
        return_value=None,
    )
    @patch(
        "research_automation.services.earnings_service.fmp_client.get_earnings_transcript",
        return_value=None,
    )
    @patch(
        "research_automation.services.earnings_service.fetch_transcript_from_8k",
    )
    def test_analyze_uses_sec_api_when_prior_sources_empty(
        self,
        mock_fetch,
        _mock_fmp,
        _mock_sec,
        _mock_ec,
        mock_chat,
    ) -> None:
        mock_fetch.return_value = (
            "Operator: Good day. This is the fourth quarter 2024 earnings conference call. "
            "Forward-looking statements apply.\n\n"
            "Tim Cook: We are pleased with record revenue for the quarter."
        )
        mock_chat.return_value = json.dumps(
            {
                "summary": "本季度营收稳健。",
                "summary_source_paragraph_ids": [],
                "management_viewpoints": [{"text": "业绩良好", "source_paragraph_ids": []}],
                "quotations": [
                    {
                        "speaker": "Tim Cook",
                        "quote": "We are pleased with record revenue for the quarter.",
                        "topic": "Results",
                        "source_paragraph_ids": [],
                    }
                ],
                "new_business_highlights": [],
            },
            ensure_ascii=False,
        )
        out = analyze_earnings_call("MSFT", 2024, 4)
        self.assertEqual(out.data_source, "sec_api")
        mock_fetch.assert_called_once()
        mock_chat.assert_called_once()

    @patch(
        "research_automation.services.earnings_service.fetch_transcript_from_8k",
        return_value=None,
    )
    @patch(
        "research_automation.services.earnings_service.search_8k_transcript",
        return_value=None,
    )
    @patch(
        "research_automation.services.earnings_service.get_transcript_from_earningscall",
        return_value="",
    )
    @patch(
        "research_automation.services.earnings_service.fmp_client.get_earnings_transcript",
        return_value=None,
    )
    def test_analyze_raises_when_no_transcript(
        self,
        _mock_fmp,
        _mock_ec,
        _mock_sec,
        _mock_fetch,
    ) -> None:
        """无任一来源逐字稿时应抛出业务错误。"""
        with self.assertRaises(EarningsAnalysisError) as ctx:
            analyze_earnings_call("ZZZZ", 2024, 1)
        self.assertIn("transcript", ctx.exception.message.lower())


if __name__ == "__main__":
    unittest.main()
