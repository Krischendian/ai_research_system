"""业务画像：模型与抽取字段校验。"""
from __future__ import annotations

import json
import unittest
from pathlib import Path
from unittest.mock import patch

from research_automation.services.profile_service import get_profile

_PROFILE_EXCERPT_FIXTURE = (
    Path(__file__).resolve().parent / "fixtures" / "profile_prompt_excerpt.txt"
).read_text(encoding="utf-8")


def _mock_10k_sections() -> dict[str, str]:
    """与旧版单文件 Item1 节选等价：测试用合并后正文仍含 fixture 全文。"""
    return {
        "item1": _PROFILE_EXCERPT_FIXTURE,
        "item1a": "",
        "item7": "",
        "item8_notes": "",
    }


def _non_placeholder(s: str) -> bool:
    return bool(s) and s not in ("原文未明确提及", "NOT_FOUND")


class TestBusinessProfileFields(unittest.TestCase):
    @patch("research_automation.services.profile_service.chat")
    @patch("research_automation.services.profile_service.get_10k_sections")
    def test_future_and_industry_fields_populated(self, mock_sections, mock_chat) -> None:
        """Mock LLM：新字段须为可展示的抽取内容（非占位）。"""
        mock_sections.return_value = _mock_10k_sections()
        mock_chat.return_value = json.dumps(
            {
                "core_business": "设计制造移动设备与个人电脑，并提供软件与服务。",
                "revenue_by_segment": [
                    {"segment_name": "手机", "percentage": "52%"},
                    {"segment_name": "服务", "percentage": "19%"},
                ],
                "revenue_by_geography": [
                    {"segment_name": "美洲", "percentage": "42%"},
                ],
                "future_guidance": "公司预计下一财季总收入介于 1180 亿至 1210 亿美元；服务收入同比中段两位数增长。",
                "industry_view": "管理层认为高端智能手机全球需求较往年放缓，换机周期拉长；主要产品类别竞争加剧。",
                "key_quotes": [
                    {
                        "speaker": "CEO",
                        "quote": (
                            "Regarding our Services segment, we expect year-over-year revenue growth "
                            "in the mid-teens, consistent with our recent trends."
                        ),
                        "topic": "Guidance Services",
                    },
                    {
                        "speaker": "CFO",
                        "quote": (
                            "For the first fiscal quarter ending December 30, 2023, we expect total company revenue "
                            "to be in the range of $118 billion to $121 billion."
                        ),
                        "topic": "Guidance",
                    },
                ],
                "corporate_actions": [
                    {
                        "action_type": "new_business",
                        "description": "推出面向企业客户的新付费云分析服务层级。",
                        "date": None,
                        "source_quote": (
                            "During the third quarter of fiscal 2024, we launched a new paid tier "
                            "of our cloud analytics platform for enterprise customers, expanding our "
                            "software subscription offerings."
                        ),
                    },
                    {
                        "action_type": "acquisition",
                        "description": "完成对 DataSense Inc. 的现金收购。",
                        "date": "June 15, 2024",
                        "source_quote": (
                            "On June 15, 2024, we completed the acquisition of DataSense Inc., "
                            "a privately held provider of on-device machine learning tools, for "
                            "approximately $450 million in cash."
                        ),
                    },
                    {
                        "action_type": "partnership",
                        "description": "与 Orion Networks 达成战略合作并联合营销安全软件。",
                        "date": None,
                        "source_quote": (
                            "We entered into a strategic partnership with Orion Networks under which "
                            "the parties will jointly market our security software to small and "
                            "medium-sized businesses in North America, as described in the agreement "
                            "filed as an exhibit to this report."
                        ),
                    },
                ],
            },
            ensure_ascii=False,
        )

        profile = get_profile("TEST")
        self.assertTrue(_non_placeholder(profile.future_guidance))
        self.assertTrue(_non_placeholder(profile.industry_view))
        self.assertIn("1180", profile.future_guidance)
        self.assertTrue("竞争" in profile.industry_view or "需求" in profile.industry_view)
        self.assertGreaterEqual(len(profile.key_quotes), 1)
        self.assertTrue(all(kq.quote for kq in profile.key_quotes))
        types_found = {a.action_type for a in profile.corporate_actions}
        self.assertEqual(types_found, {"new_business", "acquisition", "partnership"})
        self.assertTrue(all(a.source_quote for a in profile.corporate_actions))
        mock_sections.assert_called_once()
        mock_chat.assert_called_once()

    @patch("research_automation.services.profile_service.chat")
    @patch("research_automation.services.profile_service.get_10k_sections")
    def test_missing_guidance_fields_get_placeholder(self, mock_sections, mock_chat) -> None:
        """Mock LLM 省略新字段时，服务端应回落到占位文案。"""
        mock_sections.return_value = _mock_10k_sections()
        mock_chat.return_value = json.dumps(
            {
                "core_business": "测试业务",
                "revenue_by_segment": [],
                "revenue_by_geography": [],
            },
            ensure_ascii=False,
        )

        profile = get_profile("X")
        self.assertEqual(profile.future_guidance, "原文未明确提及")
        self.assertEqual(profile.industry_view, "原文未明确提及")
        self.assertEqual(profile.key_quotes, [])
        self.assertEqual(profile.corporate_actions, [])

    @patch("research_automation.services.profile_service.chat")
    @patch("research_automation.services.profile_service.get_10k_sections")
    def test_item1_rule_fallback_fills_revenue_mix(self, mock_sections, mock_chat) -> None:
        """模型未返回分部/地区时，从 Item 1 原文规则解析补全。"""
        mock_sections.return_value = _mock_10k_sections()
        mock_chat.return_value = json.dumps(
            {
                "core_business": "测试业务",
                "revenue_by_segment": [],
                "revenue_by_geography": [],
                "future_guidance": None,
                "industry_view": None,
                "key_quotes": [],
                "corporate_actions": [],
                "field_sources": {
                    "core_business": [],
                    "future_guidance": [],
                    "industry_view": [],
                },
            },
            ensure_ascii=False,
        )
        profile = get_profile("MIX")
        names = {s.segment_name for s in profile.revenue_by_segment}
        self.assertIn("iPhone", names)
        self.assertIn("Services", names)
        geos = {g.segment_name for g in profile.revenue_by_geography}
        self.assertIn("Americas", geos)
        self.assertIn("Greater China", geos)
        self.assertIn("规则解析补全", profile.data_source_label)

    @patch("research_automation.services.profile_service.chat")
    @patch("research_automation.services.profile_service.get_10k_sections")
    def test_item8_dollar_table_fallback(self, mock_sections, mock_chat) -> None:
        """Item 7 内「dollars in millions」表：按金额推算占比。"""
        tbl = (
            "The following table shows net sales by category for 2025, 2024 and 2023 "
            "(dollars in millions): iPhone $ 209,586 Mac 33,708 iPad 28,023 "
            "Wearables, Home and Accessories 35,686 Services (1) 109,158 "
            "Total net sales $ 416,161\n"
            "The following table shows net sales by reportable segment for 2025, 2024 "
            "and 2023 (dollars in millions): Americas $ 178,353 Europe 111,032 "
            "Greater China 64,377 Japan 28,703 Rest of Asia Pacific 33,696 "
            "Total net sales $ 416,161\n"
        )
        mock_sections.return_value = {
            "item1": "Item 1. Business\nShort.\n",
            "item1a": "",
            "item7": tbl,
            "item8_notes": "",
        }
        mock_chat.return_value = json.dumps(
            {
                "core_business": "测试",
                "revenue_by_segment": [],
                "revenue_by_geography": [],
                "future_guidance": None,
                "industry_view": None,
                "key_quotes": [],
                "corporate_actions": [],
                "field_sources": {
                    "core_business": [],
                    "future_guidance": [],
                    "industry_view": [],
                },
            },
            ensure_ascii=False,
        )
        profile = get_profile("TBL")
        self.assertGreaterEqual(len(profile.revenue_by_segment), 1)
        self.assertGreaterEqual(len(profile.revenue_by_geography), 1)
        self.assertTrue(
            any(s.segment_name == "iPhone" for s in profile.revenue_by_segment)
        )
        self.assertTrue(
            any(g.segment_name == "Americas" for g in profile.revenue_by_geography)
        )
        self.assertIn("Net sales", profile.data_source_label)

    @patch("research_automation.services.profile_service.chat")
    @patch("research_automation.services.profile_service.get_10k_sections")
    def test_key_quotes_non_verbatim_dropped(self, mock_sections, mock_chat) -> None:
        """节选内不存在的 quote 须在服务端丢弃，不得入库。"""
        mock_sections.return_value = _mock_10k_sections()
        mock_chat.return_value = json.dumps(
            {
                "core_business": "x",
                "revenue_by_segment": [{"segment_name": "A", "percentage": "1%"}],
                "revenue_by_geography": [],
                "future_guidance": "原文未明确提及",
                "industry_view": "原文未明确提及",
                "key_quotes": [
                    {
                        "speaker": "CEO",
                        "quote": "This sentence was never in the filing excerpt.",
                        "topic": "Fake",
                    }
                ],
            },
            ensure_ascii=False,
        )

        profile = get_profile("BADQUOTE")
        self.assertEqual(profile.key_quotes, [])

    @patch("research_automation.services.profile_service.chat")
    @patch("research_automation.services.profile_service.get_10k_sections")
    def test_corporate_actions_non_verbatim_dropped(self, mock_sections, mock_chat) -> None:
        """source_quote 不在节选内则整条动态丢弃。"""
        mock_sections.return_value = _mock_10k_sections()
        mock_chat.return_value = json.dumps(
            {
                "core_business": "x",
                "revenue_by_segment": [{"segment_name": "A", "percentage": "1%"}],
                "revenue_by_geography": [],
                "future_guidance": "原文未明确提及",
                "industry_view": "原文未明确提及",
                "key_quotes": [],
                "corporate_actions": [
                    {
                        "action_type": "acquisition",
                        "description": "假收购",
                        "date": None,
                        "source_quote": "This acquisition was never disclosed in the excerpt.",
                    }
                ],
            },
            ensure_ascii=False,
        )

        profile = get_profile("BADACTION")
        self.assertEqual(profile.corporate_actions, [])

    @patch("research_automation.services.profile_service.get_segment_revenue")
    @patch("research_automation.services.profile_service.chat")
    @patch("research_automation.services.profile_service.get_10k_sections")
    def test_fmp_segment_override_when_empty(
        self, mock_sections, mock_chat, mock_fmp_seg
    ) -> None:
        """无业务线占比时以 FMP 分部数据覆盖。"""
        mock_sections.return_value = {
            "item1": "No segment percents here.\n",
            "item1a": "",
            "item7": "",
            "item8_notes": "",
        }
        mock_chat.return_value = json.dumps(
            {
                "core_business": "测试",
                "revenue_by_segment": [],
                "revenue_by_geography": [],
                "future_guidance": None,
                "industry_view": None,
                "key_quotes": [],
                "corporate_actions": [],
                "field_sources": {
                    "core_business": [],
                    "future_guidance": [],
                    "industry_view": [],
                },
            },
            ensure_ascii=False,
        )
        mock_fmp_seg.return_value = [
            {"segment": "Alpha", "percentage": 60.0, "absolute": 600.0},
            {"segment": "Beta", "percentage": 40.0, "absolute": 400.0},
        ]
        profile = get_profile("FMPFILL")
        self.assertEqual(len(profile.revenue_by_segment), 2)
        names = {s.segment_name for s in profile.revenue_by_segment}
        self.assertEqual(names, {"Alpha", "Beta"})
        self.assertIn("FMP Revenue Product Segmentation", profile.data_source_label)
        self.assertIsNone(profile.validation_warning)

    @patch("research_automation.services.profile_service.get_segment_revenue")
    @patch("research_automation.services.profile_service.chat")
    @patch("research_automation.services.profile_service.get_10k_sections")
    def test_fmp_segment_validation_warning_large_gap(
        self, mock_sections, mock_chat, mock_fmp_seg
    ) -> None:
        """与 FMP 能对齐的分部占比差超过 10% 时写入 validation_warning。"""
        mock_sections.return_value = {
            "item1": "Plain.\n",
            "item1a": "",
            "item7": "",
            "item8_notes": "",
        }
        mock_chat.return_value = json.dumps(
            {
                "core_business": "测试",
                "revenue_by_segment": [{"segment_name": "iPhone", "percentage": "15%"}],
                "revenue_by_geography": [],
                "future_guidance": None,
                "industry_view": None,
                "key_quotes": [],
                "corporate_actions": [],
                "field_sources": {
                    "core_business": [],
                    "future_guidance": [],
                    "industry_view": [],
                },
            },
            ensure_ascii=False,
        )
        mock_fmp_seg.return_value = [
            {"segment": "iPhone", "percentage": 50.0, "absolute": 500.0},
            {"segment": "Mac", "percentage": 50.0, "absolute": 500.0},
        ]
        profile = get_profile("FMPWARN")
        self.assertEqual(profile.validation_warning, "业务线占比与财报披露偏差较大，请人工复核")
        self.assertEqual(profile.revenue_by_segment[0].segment_name, "iPhone")
        self.assertEqual(profile.revenue_by_segment[0].percentage, "15%")


if __name__ == "__main__":
    unittest.main()
