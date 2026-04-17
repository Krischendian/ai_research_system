"""内部人士交易：FMP ``insider-trading/search`` 汇总。"""
from __future__ import annotations

import logging
from collections import Counter
from datetime import date, datetime, timedelta
from typing import Any

from research_automation.extractors import fmp_client

logger = logging.getLogger(__name__)


def _parse_iso_date(s: str | None) -> date | None:
    if not s:
        return None
    raw = str(s).strip()[:10]
    if len(raw) < 10:
        return None
    try:
        return datetime.strptime(raw, "%Y-%m-%d").date()
    except ValueError:
        return None


def _floaty(v: object) -> float | None:
    """安全转 ``float``；无效或 NaN 返回 ``None``。"""
    if v is None:
        return None
    try:
        x = float(v)  # type: ignore[arg-type]
    except (TypeError, ValueError):
        return None
    if x != x:  # NaN
        return None
    return x


def _notional_for_trade(r: dict[str, Any]) -> float | None:
    """
    单笔名义金额：优先 ``totalValue``；缺失时用 ``shares * price``。
    仍无法得到有效数值时返回 ``None``（展示层可写「股数未披露」）。
    """
    tv = _floaty(r.get("totalValue"))
    if tv is not None and tv > 0:
        return tv
    sh = _floaty(r.get("shares"))
    px = _floaty(r.get("price"))
    if sh is not None and px is not None and sh > 0 and px > 0:
        return sh * px
    if tv is not None and tv == 0:
        return 0.0
    return None


def get_insider_summary(ticker: str, days_back: int = 30) -> dict[str, Any]:
    """
    拉取内部交易并筛选 ``transactionDate``（缺省则用 ``filingDate``）在
    最近 ``days_back`` 日内的记录，统计买卖笔数与名义金额、主要内部人士。

    金额优先 ``totalValue``，否则用股数×成交价推算；若该侧有成交但全部无法推算金额，
    则 ``total_buy_value`` / ``total_sell_value`` 为 ``None``，由报告写「股数未披露」。
    """
    sym = (ticker or "").strip().upper()
    if not sym:
        return {
            "ticker": "",
            "days_back": days_back,
            "trade_count": 0,
            "buy_count": 0,
            "sell_count": 0,
            "other_count": 0,
            "total_buy_value": None,
            "total_sell_value": None,
            "net_value": None,
            "top_insiders": [],
            "trades": [],
        }

    try:
        rows = fmp_client.get_insider_trades(sym, limit=max(50, days_back * 3))
    except Exception:
        logger.exception("内部交易拉取异常 ticker=%s", sym)
        rows = []

    cutoff = date.today() - timedelta(days=max(1, int(days_back)))
    filtered: list[dict[str, Any]] = []
    for r in rows:
        td = _parse_iso_date(str(r.get("transactionDate") or ""))
        if td is None:
            td = _parse_iso_date(str(r.get("filingDate") or ""))
        if td is None or td < cutoff:
            continue
        filtered.append(r)

    buy_c = sell_c = other_c = 0
    buy_val = 0.0
    sell_val = 0.0
    buy_any = sell_any = False
    insider_values: dict[str, float] = {}
    insider_counts: Counter[str] = Counter()
    insider_has_notional: dict[str, bool] = {}

    for r in filtered:
        side = str(r.get("transactionType") or "").strip()
        name = str(r.get("insiderName") or "").strip() or "UNKNOWN"
        n = _notional_for_trade(r)

        if side == "Buy":
            buy_c += 1
            if n is not None:
                buy_any = True
                buy_val += float(n)
        elif side == "Sell":
            sell_c += 1
            if n is not None:
                sell_any = True
                sell_val += float(n)
        else:
            other_c += 1

        insider_counts[name] += 1
        if n is not None:
            insider_has_notional[name] = True
            insider_values[name] = insider_values.get(name, 0.0) + float(n)

    top: list[dict[str, Any]] = []
    for name, cnt in insider_counts.most_common(8):
        has_n = insider_has_notional.get(name, False)
        tv = insider_values.get(name) if has_n else None
        top.append(
            {
                "insiderName": name,
                "trades": int(cnt),
                "total_value": float(tv) if tv is not None else None,
            }
        )

    total_buy_value = buy_val if buy_any else (None if buy_c > 0 else None)
    total_sell_value = sell_val if sell_any else (None if sell_c > 0 else None)
    # 任一侧有笔数但金额全部不可算时，净买卖不展示，避免误导读数
    net_value = None
    if (buy_c > 0 and not buy_any) or (sell_c > 0 and not sell_any):
        net_value = None
    elif buy_any or sell_any:
        net_value = (total_buy_value or 0.0) - (total_sell_value or 0.0)

    return {
        "ticker": sym,
        "days_back": int(days_back),
        "trade_count": len(filtered),
        "buy_count": buy_c,
        "sell_count": sell_c,
        "other_count": other_c,
        "total_buy_value": total_buy_value,
        "total_sell_value": total_sell_value,
        "net_value": net_value,
        "top_insiders": top,
        "trades": filtered,
    }
