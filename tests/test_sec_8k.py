"""SEC 8-K 逐字稿烟测。

- EDGAR 直连：需 ``SEC_EDGAR_USER_AGENT``。
- sec-api.io：需 ``SEC_API_KEY``。

从项目根执行::

    PYTHONPATH=src python3 tests/test_sec_8k.py
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

from research_automation.extractors.sec_8k_client import (  # noqa: E402
    fetch_transcript_from_8k,
    search_8k_transcript,
)


def test_search_8k_empty_ticker() -> None:
    assert search_8k_transcript("", lookback_days=14) is None


def test_fetch_transcript_from_8k_no_api_key(monkeypatch) -> None:
    monkeypatch.delenv("SEC_API_KEY", raising=False)
    assert fetch_transcript_from_8k("AAPL", lookback_days=14) is None


def main() -> None:
    ticker = "AAPL"

    if (os.environ.get("SEC_API_KEY") or "").strip():
        print("[sec-api.io] 尝试 fetch_transcript_from_8k（最近 14 天）…")
        text = fetch_transcript_from_8k(ticker, lookback_days=14)
        if text:
            preview = text[:500] + ("…" if len(text) > 500 else "")
            print(
                f"[sec-api.io] 命中，长度={len(text)} 字符，前 500 字：\n{preview}"
            )
            return
        print("[sec-api.io] 未命中，继续尝试 EDGAR…")
    else:
        print("[sec-api.io] 未设置 SEC_API_KEY，跳过。")

    if not (os.environ.get("SEC_EDGAR_USER_AGENT") or "").strip():
        print(
            "[跳过 EDGAR] 未设置 SEC_EDGAR_USER_AGENT；"
            "请在 .env 中配置后再试 EDGAR 路径。"
        )
        return

    text = search_8k_transcript(ticker, lookback_days=14)
    if text:
        preview = text[:500] + ("…" if len(text) > 500 else "")
        print(
            f"[EDGAR 8-K] 14 天窗口命中，长度={len(text)} 字符，前 500 字：\n{preview}"
        )
        return

    print(
        f"[EDGAR 8-K] 最近 14 天内无可用 EX-99 类长文（ticker={ticker}），"
        "扩大至 120 天窗口重试（仅烟测）…"
    )
    text = search_8k_transcript(ticker, lookback_days=120)
    if not text:
        print(
            "[EDGAR 8-K] 120 天内仍无命中。可配置 SEC_API_KEY 试 sec-api，"
            "或使用带财季的 earnings API。"
        )
        return

    preview = text[:500] + ("…" if len(text) > 500 else "")
    print(
        f"[EDGAR 8-K] 120 天窗口命中，长度={len(text)} 字符，前 500 字：\n{preview}"
    )


if __name__ == "__main__":
    main()
