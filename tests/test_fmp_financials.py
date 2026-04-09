"""FMP 标准化财务烟测。从项目根执行::

    PYTHONPATH=src python tests/test_fmp_financials.py

有密钥且 FMP 返回数据时校验 ``data_source=FMP``；密钥无效 / 403 / 限额导致 FMP 为空时允许回退 SEC，不因「仅有密钥」强行断言 FMP。
"""
from __future__ import annotations

import os
import sys
from pathlib import Path
from pprint import pprint

_ROOT = Path(__file__).resolve().parents[1]
_SRC = _ROOT / "src"
if str(_SRC) not in sys.path:
    sys.path.insert(0, str(_SRC))

from dotenv import load_dotenv  # noqa: E402

load_dotenv(_ROOT / ".env", override=False)

from research_automation.extractors.fmp_client import get_financials as fmp_get_financials  # noqa: E402
from research_automation.services.financial_service import get_financials  # noqa: E402


def test_fmp_client_empty_ticker() -> None:
    assert fmp_get_financials("", years=3) == []
    assert fmp_get_financials("   ", years=3) == []


def test_fmp_client_zero_years() -> None:
    assert fmp_get_financials("AAPL", years=0) == []


def main() -> None:
    ticker = "AAPL"
    has_key = bool((os.getenv("FMP_API_KEY") or "").strip())

    raw = fmp_get_financials(ticker, years=3)
    print(f"[FMP 直连] ticker={ticker} 行数={len(raw)} (有 API Key: {has_key})")
    for row in raw:
        pprint(row.model_dump())

    bundle = get_financials(ticker)
    print(
        f"\n[financial_service] data_source={bundle.data_source!r} "
        f"行数={len(bundle.financials)}"
    )
    for row in bundle.financials:
        pprint(row.model_dump())

    assert bundle.ticker == ticker
    assert len(bundle.financials) <= 3

    if raw:
        assert bundle.data_source == "FMP", "FMP 有数据时应走 FMP"
        assert len(bundle.financials) >= 1
        for r in bundle.financials:
            assert r.year >= 2000
            if r.revenue is not None:
                assert r.revenue > 0
            if r.gross_margin is not None:
                assert 0 <= r.gross_margin <= 1.5
    else:
        if not has_key:
            assert raw == [], "无密钥时 FMP 客户端应返回空列表"
        else:
            print(
                "\n[提示] 已配置 FMP_API_KEY 但 FMP 无数据（端点/套餐/403 等），"
                "已回退 SEC；当前客户端使用 /stable/ 与 period=annual。"
            )
        if bundle.financials:
            assert bundle.data_source == "SEC", "FMP 无数据时应回退 SEC"

    print("\n[OK] 检查通过。")


if __name__ == "__main__":
    main()
