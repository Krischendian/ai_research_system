"""FMP 财务 / 逐字稿 / 内部交易集成烟测。

无 ``FMP_API_KEY`` 时跳过依赖网络的用例；带 mock 的用例始终可跑。

从项目根::

    PYTHONPATH=src python -m pytest tests/test_fmp_integration.py -q
    PYTHONPATH=src python tests/test_fmp_integration.py
"""
from __future__ import annotations

import json
import os
import sys
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

_ROOT = Path(__file__).resolve().parents[1]
_SRC = _ROOT / "src"
if str(_SRC) not in sys.path:
    sys.path.insert(0, str(_SRC))

from dotenv import load_dotenv  # noqa: E402

load_dotenv(_ROOT / ".env", override=False)


@pytest.mark.skipif(
    not (os.getenv("FMP_API_KEY") or "").strip(),
    reason="FMP_API_KEY not set",
)
def test_live_get_financials_aapl() -> None:
    from research_automation.extractors.fmp_client import get_financials

    rows = get_financials("AAPL", years=3)
    print("\n[FMP get_financials AAPL]", len(rows), "years")
    for r in rows:
        print(r.model_dump())
    assert len(rows) >= 1
    assert all(2000 <= r.year <= 2100 for r in rows)


@pytest.mark.skipif(
    not (os.getenv("FMP_API_KEY") or "").strip(),
    reason="FMP_API_KEY not set",
)
def test_live_get_earnings_transcript_aapl_2024q4() -> None:
    from research_automation.extractors.fmp_client import get_earnings_transcript

    tr = get_earnings_transcript("AAPL", 2024, 4)
    if tr is None:
        pytest.skip("FMP 无该季逐字稿或套餐不含该端点")
    text_blob = json.dumps(tr.get("content") or [], ensure_ascii=False)
    preview = text_blob[:500]
    print("\n[FMP transcript AAPL 2024Q4] first 500 chars:\n", preview)
    assert tr.get("quarter") == "2024Q4"
    assert isinstance(tr.get("content"), list)
    assert len(tr["content"]) >= 1
    assert "text" in tr["content"][0]


@pytest.mark.skipif(
    not (os.getenv("FMP_API_KEY") or "").strip(),
    reason="FMP_API_KEY not set",
)
def test_live_get_insider_trades_aapl() -> None:
    from research_automation.extractors.fmp_client import get_insider_trades

    rows = get_insider_trades("AAPL", limit=10)
    print("\n[FMP insider AAPL limit=10]", rows)
    assert isinstance(rows, list)
    if rows:
        assert "transactionDate" in rows[0]
        assert "insiderName" in rows[0]


@patch("research_automation.extractors.fmp_client.get_insider_trades")
def test_insider_summary_aggregates(mock_trades: MagicMock) -> None:
    from datetime import date, timedelta

    from research_automation.services.insider_service import get_insider_summary

    d0 = (date.today() - timedelta(days=5)).isoformat()
    d1 = (date.today() - timedelta(days=3)).isoformat()
    mock_trades.return_value = [
        {
            "transactionDate": d0,
            "filingDate": d0,
            "insiderName": "Alice",
            "transactionType": "Buy",
            "shares": 10.0,
            "price": 5.0,
            "totalValue": 50.0,
        },
        {
            "transactionDate": d1,
            "filingDate": d1,
            "insiderName": "Bob",
            "transactionType": "Sell",
            "shares": 2.0,
            "price": 100.0,
            "totalValue": 200.0,
        },
    ]
    out = get_insider_summary("AAPL", days_back=30)
    assert out["trade_count"] == 2
    assert out["buy_count"] == 1 and out["sell_count"] == 1
    assert out["total_buy_value"] == 50.0
    assert out["total_sell_value"] == 200.0
    assert out["net_value"] == -150.0


@patch("research_automation.extractors.fmp_client._api_key", return_value="x")
@patch("research_automation.extractors.fmp_client.requests.get")
def test_insider_trades_parses_stable_search(mock_get: MagicMock, _k: str) -> None:
    from research_automation.extractors.fmp_client import get_insider_trades

    mock_get.return_value.raise_for_status = MagicMock()
    mock_get.return_value.json.return_value = [
        {
            "symbol": "AAPL",
            "filingDate": "2026-01-02",
            "transactionDate": "2025-12-30",
            "reportingName": "Jane Doe",
            "typeOfOwner": "officer",
            "transactionType": "S-Sale",
            "acquisitionOrDisposition": "D",
            "securitiesTransacted": 100,
            "price": 10.0,
            "securityName": "Common Stock",
            "url": "https://www.sec.gov/example",
        }
    ]
    rows = get_insider_trades("AAPL", limit=10)
    assert len(rows) == 1
    assert rows[0]["insiderName"] == "Jane Doe"
    assert rows[0]["transactionType"] == "Sell"
    assert rows[0]["shares"] == 100.0
    assert rows[0]["price"] == 10.0
    assert rows[0]["totalValue"] == 1000.0


def main() -> None:
    """手动运行：打印 AAPL 财务 / 逐字稿片段 / 内部交易（需密钥与网络）。"""
    if not (os.getenv("FMP_API_KEY") or "").strip():
        print("跳过：未配置 FMP_API_KEY")
        return
    from research_automation.extractors.fmp_client import (
        get_earnings_transcript,
        get_financials,
        get_insider_trades,
    )
    from research_automation.services.financial_service import get_financials as svc_get
    from research_automation.services.insider_service import get_insider_summary

    print("=== fmp_client.get_financials('AAPL') ===")
    for r in get_financials("AAPL", years=3):
        print(r.model_dump())

    print("\n=== financial_service.get_financials('AAPL') ===")
    b = svc_get("AAPL")
    print("data_source:", b.data_source, "rows:", len(b.financials))

    tr = get_earnings_transcript("AAPL", 2024, 4)
    print("\n=== get_earnings_transcript AAPL 2024Q4 ===")
    if tr:
        blob = json.dumps(tr.get("content") or [], ensure_ascii=False)
        print(blob[:500])
    else:
        print("None")

    print("\n=== get_insider_trades AAPL limit=10 ===")
    print(get_insider_trades("AAPL", limit=10))

    print("\n=== get_insider_summary AAPL 30d ===")
    print(get_insider_summary("AAPL", days_back=30))


if __name__ == "__main__":
    main()
