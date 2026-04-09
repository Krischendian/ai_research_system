"""FMP 财报电话会逐字稿烟测。从项目根执行::

    PYTHONPATH=src python3 tests/test_fmp_earnings.py

需有效 ``FMP_API_KEY`` 且 **套餐含 Earnings Call Transcript** 时，应打印带 speaker 的对话条目。
若出现 HTTP 402，属 FMP 付费数据集，可升级套餐或依赖后端自动回退 ``earningscall``。
"""
from __future__ import annotations

import os
import sys
from pathlib import Path

_ROOT = Path(__file__).resolve().parents[1]
_SRC = _ROOT / "src"
if str(_SRC) not in sys.path:
    sys.path.insert(0, str(_SRC))

from dotenv import load_dotenv  # noqa: E402

load_dotenv(_ROOT / ".env", override=False)

from research_automation.extractors.fmp_client import get_earnings_transcript  # noqa: E402


def test_get_earnings_transcript_invalid_quarter() -> None:
    assert get_earnings_transcript("AAPL", 2024, 0) is None
    assert get_earnings_transcript("AAPL", 2024, 5) is None


def main() -> None:
    ticker = "AAPL"
    year, quarter = 2024, 3
    tr = get_earnings_transcript(ticker, year, quarter)
    has_key = bool((os.getenv("FMP_API_KEY") or "").strip())

    if not tr:
        print(
            f"[FMP 逐字稿] 无数据 ticker={ticker} {year}Q{quarter} "
            f"(已配置 API Key: {has_key})"
        )
        if has_key:
            print(
                "      说明：若日志为 HTTP 402 Payment Required，表示当前 FMP 计划不含逐字稿接口；"
                "``analyze_earnings_call`` 会自动使用 earningscall 库。"
            )
        return

    print(f"[FMP 逐字稿] quarter={tr.get('quarter')} date={tr.get('date')}")
    content = tr.get("content") or []
    print(f"对话条数: {len(content)}，前 5 条：")
    for i, row in enumerate(content[:5], start=1):
        sp = (row.get("speaker") or "").strip()
        pos = (row.get("position") or "").strip()
        tx = (row.get("text") or "").strip()
        preview = (tx[:200] + "…") if len(tx) > 200 else tx
        print(f"  {i}. speaker={sp!r} position={pos!r}\n     text={preview!r}")

    if has_key and content:
        assert any(
            (str(r.get("speaker") or "").strip() or str(r.get("text") or "").strip())
            for r in content
        ), "至少应有 speaker 或 text"


if __name__ == "__main__":
    main()
