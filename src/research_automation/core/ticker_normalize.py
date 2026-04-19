"""路径参数中的股票代码规范化（大小写 + 常见拼写纠错 + 交易所后缀处理）。"""
from __future__ import annotations
import re

# 用户易混：少打一个 A → SEC CIK 映射失败
_TICKER_TYPOS: dict[str, str] = {
    "APPL": "AAPL",
}

# 交易所后缀 → 标准化后缀映射
# 支持 Bloomberg 格式：BT/A LN、FRE GY、DHL GY、KBX GY
_EXCHANGE_SUFFIX_MAP: dict[str, str] = {
    "LN": "LN",   # London
    "GY": "GY",   # Germany XETRA
    "FP": "FP",   # France
    "JP": "JP",   # Japan
    "HK": "HK",   # Hong Kong
    "AU": "AU",   # Australia
    "CN": "CN",   # Canada
    "SS": "SS",   # Shanghai
    "SZ": "SZ",   # Shenzhen
}

# Bloomberg ticker → FMP/内部可用 ticker 映射
# 非美股在FMP无逐字稿，但财务数据可能有ISIN查询
_BLOOMBERG_TO_FMP: dict[str, str] = {
    "BT/A LN": "BT-A.L",
    "FRE GY": "FRE.DE",
    "KBX GY": "KBX.DE",
    "DHL GY": "DHL.DE",
}

# 缓存路径安全化：把非法字符替换为下划线
_UNSAFE_CHARS = re.compile(r"[/\\:*?\"<>|. ]")


def normalize_equity_ticker(raw: str) -> str:
    """标准化ticker：去空格、转大写、修正拼写。保留交易所后缀。"""
    s = (raw or "").strip().upper()
    if not s:
        return s
    return _TICKER_TYPOS.get(s, s)


def bloomberg_to_fmp(bloomberg_ticker: str) -> str | None:
    """
    将Bloomberg格式ticker转换为FMP可用格式。
    例如：'BT/A LN' → 'BT-A.L'
    无映射时返回None。
    """
    key = (bloomberg_ticker or "").strip().upper()
    # 先查精确映射
    for k, v in _BLOOMBERG_TO_FMP.items():
        if k.upper() == key:
            return v
    return None


def is_us_equity(ticker: str) -> bool:
    """
    判断是否为美股（无交易所后缀 且 不含特殊字符）。
    BT/A LN → False，ACN → True
    """
    t = (ticker or "").strip()
    # 有空格说明有交易所后缀（Bloomberg格式）
    if " " in t:
        return False
    # 含 . 说明是非美股FMP格式（BT-A.L）
    if "." in t:
        return False
    return True


def ticker_to_cache_key(ticker: str) -> str:
    """
    将ticker转换为安全的文件系统缓存key。
    BT/A LN → BT_A_LN，FRE GY → FRE_GY
    """
    return _UNSAFE_CHARS.sub("_", (ticker or "").strip()).upper()


def get_fmp_symbol(ticker: str) -> str:
    """
    获取用于FMP API调用的symbol。
    美股直接返回，非美股尝试bloomberg_to_fmp映射，无映射返回原始ticker。
    """
    if is_us_equity(ticker):
        return normalize_equity_ticker(ticker)
    fmp = bloomberg_to_fmp(ticker)
    return fmp if fmp else ticker.strip()
