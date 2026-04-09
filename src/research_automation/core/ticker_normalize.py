"""路径参数中的股票代码规范化（大小写 + 常见拼写纠错）。"""
from __future__ import annotations

# 用户易混：少打一个 A → SEC CIK 映射失败
_TICKER_TYPOS: dict[str, str] = {
    "APPL": "AAPL",
}


def normalize_equity_ticker(raw: str) -> str:
    s = (raw or "").strip().upper()
    if not s:
        return s
    return _TICKER_TYPOS.get(s, s)
