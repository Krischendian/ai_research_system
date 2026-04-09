"""新闻聚类与重要性：mock LLM，验证解析与 top_news 过滤。"""
from __future__ import annotations

import json
import unittest
from unittest.mock import patch

from research_automation.services import news_insights


class TestNewsInsights(unittest.TestCase):
    """``compute_news_insights`` 对固定 JSON 的展开与高分列表。"""

    @patch("research_automation.services.news_insights.chat")
    @patch("research_automation.services.news_insights._read_cache")
    def test_cluster_expand_and_top_news(self, mock_cache, mock_chat) -> None:
        """模型返回两聚类、部分高分；应展开为 ``NewsCluster`` 且 top_news≥7。"""
        mock_cache.return_value = None
        mock_chat.return_value = json.dumps(
            {
                "clusters": [
                    {
                        "cluster_id": "apple_fold",
                        "representative_title": "苹果折叠屏传言汇总",
                        "importance_score": 8,
                        "item_indices": [0, 1],
                    },
                    {
                        "cluster_id": "fed_rates",
                        "representative_title": "美联储利率路径",
                        "importance_score": 9,
                        "item_indices": [2],
                    },
                ],
                "item_scores": [
                    {"index": 0, "score": 7},
                    {"index": 1, "score": 8},
                    {"index": 2, "score": 9},
                ],
                "analyst_briefing": "市场聚焦流动性与龙头供应链预期，注意折叠屏题材反复交易。",
            },
            ensure_ascii=False,
        )

        flat = [
            {
                "title": "Apple fold delay rumor",
                "summary": "Supply chain note",
                "source_url": "https://a.example/1",
                "source": "Finnhub-X",
                "published_at": "2024-04-03T12:00:00Z",
                "matched_tickers": ["AAPL"],
            },
            {
                "title": "Apple September foldable launch",
                "summary": "Analyst view",
                "source_url": "https://a.example/2",
                "source": "Finnhub-Y",
                "published_at": "2024-04-03T13:00:00Z",
                "matched_tickers": ["AAPL"],
            },
            {
                "title": "Fed holds",
                "summary": "Policy",
                "source_url": "https://m.example/1",
                "source": "Reuters",
                "published_at": "2024-04-03T14:00:00Z",
                "matched_tickers": [],
            },
        ]

        clusters, top_news, briefing, smap = news_insights.compute_news_insights(
            flat,
            context="test_ctx",
            date_key="2024-04-03",
            monitor_tickers=["AAPL"],
        )

        self.assertEqual(len(clusters), 2)
        self.assertEqual(clusters[0].cluster_id, "apple_fold")
        self.assertEqual(len(clusters[0].news_items), 2)
        self.assertGreaterEqual(clusters[0].news_items[0].importance_score, 7)
        self.assertTrue(briefing)
        self.assertGreaterEqual(len(top_news), 1)
        for t in top_news:
            self.assertGreaterEqual(t.importance_score, 7)
        self.assertIn(2, smap)
        self.assertEqual(smap[2], 9)

    @patch("research_automation.services.news_insights.chat")
    @patch("research_automation.services.news_insights._read_cache")
    def test_singleton_coverage_for_missing_index(self, mock_cache, mock_chat) -> None:
        """模型漏掉某一编号时应自动补单条聚类。"""
        mock_cache.return_value = None
        mock_chat.return_value = json.dumps(
            {
                "clusters": [
                    {
                        "cluster_id": "only_one",
                        "representative_title": "仅一条",
                        "importance_score": 6,
                        "item_indices": [0],
                    }
                ],
                "item_scores": [{"index": 0, "score": 6}],
                "analyst_briefing": "占位。",
            },
            ensure_ascii=False,
        )
        flat = [
            {
                "title": "A",
                "summary": "s",
                "source_url": None,
                "source": "S",
                "published_at": None,
                "matched_tickers": [],
            },
            {
                "title": "B",
                "summary": "t",
                "source_url": None,
                "source": "S",
                "published_at": None,
                "matched_tickers": [],
            },
        ]
        clusters, _, _, _ = news_insights.compute_news_insights(
            flat,
            context="test_cov",
            date_key="2024-04-04",
            monitor_tickers=[],
        )
        titles_covered = {it.title for c in clusters for it in c.news_items}
        self.assertEqual(titles_covered, {"A", "B"})


if __name__ == "__main__":
    unittest.main()
