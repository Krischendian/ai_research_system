"""
混合数据源端到端烟测：进程内 FastAPI TestClient（无需单独起 uvicorn）。

运行（项目根）::

    PYTHONPATH=src python3 tests/test_e2e_mixed.py
"""
from __future__ import annotations

import json
import sys
import unittest
from pathlib import Path
from unittest.mock import patch

_ROOT = Path(__file__).resolve().parents[1]
_SRC = _ROOT / "src"
if str(_SRC) not in sys.path:
    sys.path.insert(0, str(_SRC))

from dotenv import load_dotenv

load_dotenv(_ROOT / ".env", override=False)

from fastapi.testclient import TestClient  # noqa: E402

from research_automation.core.company_manager import CompanyRecord  # noqa: E402
from research_automation.main import app  # noqa: E402


class TestMixedSourceE2E(unittest.TestCase):
    """验证财务 SEC、电话会 earningscall、晨报 Finnhub/RSS 契约。"""

    def setUp(self) -> None:
        self.client = TestClient(app)

    def test_financials_aapl(self) -> None:
        """财务接口返回结构正确；有行则 data_source 为 SEC EDGAR。"""
        r = self.client.get("/api/v1/companies/AAPL/financials")
        self.assertEqual(r.status_code, 200, r.text)
        data = r.json()
        self.assertEqual(data.get("ticker"), "AAPL")
        self.assertIn("financials", data)
        fin = data.get("financials") or []
        if fin:
            self.assertEqual(data.get("data_source"), "SEC EDGAR")
        else:
            self.assertIsNone(data.get("data_source"))

    def test_earnings_aapl_2024q4(self) -> None:
        """电话会：有逐字稿则 200 且 data_source 为 fmp / sec_8k / earningscall，否则 503。"""
        r = self.client.get(
            "/api/v1/companies/AAPL/earnings",
            params={"quarter": "2024Q4"},
        )
        self.assertIn(r.status_code, (200, 503), r.text)
        if r.status_code == 200:
            body = r.json()
            self.assertIn(
                body.get("data_source"),
                ("fmp", "sec_8k", "earningscall", "sec_api"),
            )
        else:
            payload = r.json()
            det = str(payload.get("detail", "")).lower()
            self.assertTrue(
                "transcript" in det or "语言模型" in det or "json" in det,
                msg=det or "(empty detail)",
            )

    @patch("research_automation.core.company_manager.list_companies")
    @patch(
        "research_automation.services.news_service.get_active_tickers",
        return_value=["AAPL"],
    )
    @patch("research_automation.services.news_service.compute_news_insights")
    @patch("research_automation.services.news_service.chat")
    @patch("research_automation.services.news_service.fetch_company_news_raw_articles_for_tickers")
    @patch("research_automation.services.news_service.fetch_rss_articles")
    @patch("research_automation.services.news_service.fetch_macro_news_with_fallback")
    def test_morning_brief_company_finnhub_macro_rss(
        self,
        mock_macro_fb,
        mock_rss,
        mock_fh,
        mock_chat,
        mock_insights,
        _mock_tickers,
        mock_lc,
    ) -> None:
        """晨报：宏观来自多源 RSS 回退链（测试中由 fetch_macro_news_with_fallback 模拟）；公司来自 Finnhub。"""
        mock_lc.return_value = [
            CompanyRecord("AAPL", "Apple Inc.", "Technology", "US", 1),
        ]
        mock_macro_fb.return_value = (
            [
                {
                    "title": "Fed holds rates",
                    "link": "https://example.com/m",
                    "description": "Policy overview.",
                    "source": "Reuters",
                    "published_at_utc": "2024-01-15T12:00:00Z",
                }
            ],
            False,
        )
        mock_rss.return_value = []
        mock_fh.return_value = [
            {
                "title": "Apple supply update",
                "link": "https://example.com/c",
                "description": "AAPL supply chain note.",
                "source": "Finnhub-Reuters",
                "published_at_utc": "2024-01-15T13:00:00Z",
                "implied_tickers": ["AAPL"],
                "finnhub_datetime_unix": 1705323600,
            }
        ]
        mock_chat.return_value = json.dumps(
            {
                "macro_news": [
                    {
                        "title": "Fed holds rates",
                        "summary": "美联储维持利率不变。",
                        "source": "Reuters",
                    }
                ],
                "company_news": [
                    {
                        "title": "Apple supply update",
                        "summary": "苹果供应链相关动态。",
                        "source": "Finnhub-Reuters",
                    }
                ],
            },
            ensure_ascii=False,
        )
        # 第二次 LLM 为聚类/评分：测试内跳过真实调用
        mock_insights.return_value = ([], [], "", {0: 7, 1: 8})
        r = self.client.get("/api/v1/news/morning-brief")
        self.assertEqual(r.status_code, 200, r.text)
        doc = r.json()
        dsl = doc.get("data_source_label") or ""
        self.assertIn("RSS", dsl)
        self.assertIn("Benzinga", dsl)
        macro = doc.get("macro_news") or []
        comp = doc.get("company_news") or []
        self.assertGreaterEqual(len(macro), 1)
        self.assertGreaterEqual(len(comp), 1)
        self.assertTrue((macro[0].get("published_at") or "").strip())
        self.assertTrue((comp[0].get("source_url") or "").strip())
        self.assertIn("AAPL", [str(x).upper() for x in (comp[0].get("matched_tickers") or [])])


def main() -> None:
    unittest.main(verbosity=2)


if __name__ == "__main__":
    main()
