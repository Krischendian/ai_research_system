"""LLM payload post-processing for overnight/daily news services."""
from __future__ import annotations

from typing import Any

# -- region correction ---------------------------------------------------------
_REGION_RULES: list[tuple[list[str], str]] = [
    (
        [
            "federal reserve",
            "fed holds",
            "fed rate",
            "fed keeps",
            "powell",
            "warsh",
            "fomc",
            "us economy",
            "us gdp",
            "us cpi",
            "us payroll",
            "us inflation",
            "trump",
            "white house",
            "congress",
            "pentagon",
            "u.s. ",
            "united states",
        ],
        "North America",
    ),
    (
        [
            "ecb",
            "european central bank",
            "boe",
            "bank of england",
            "bundesbank",
            "eurozone",
            "euro area",
            "uk economy",
            "britain",
            "germany",
            "france",
            "italy",
            "spain",
            "netherlands",
            "nordic",
            "swiss",
            "europe",
        ],
        "Europe",
    ),
    (
        [
            "iran",
            "hormuz",
            "opec",
            "middle east",
            "saudi",
            "gulf",
            "israel",
            "hezbollah",
            "uae",
            "qatar",
            "iraq",
            "syria",
            "tehran",
            "persian gulf",
        ],
        "Middle East",
    ),
    (
        [
            "china",
            "japan",
            "korea",
            "india",
            "asia",
            "samsung",
            "boj",
            "pboc",
            "rbi ",
            "nikkei",
            "hang seng",
            "renminbi",
            "yuan",
            "yen",
            "rupee",
            "won",
            "taiwan",
            "asean",
            "beijing",
            "tokyo",
            "seoul",
            "mumbai",
            "jakarta",
        ],
        "Asia",
    ),
    (
        [
            "brazil",
            "argentina",
            "mexico",
            "latin america",
            "south america",
            "chile",
            "colombia",
        ],
        "Global",
    ),
]


def _fix_region(item: dict[str, Any]) -> dict[str, Any]:
    """
    代码层强制修正明显错误的 region。
    规则：title + summary 关键词匹配，按优先级顺序（顺序即优先级）。
    无匹配则保留 LLM 原值；原值不在合法集合内则改为 Global。
    """
    valid = {"North America", "Europe", "Middle East", "Asia", "Global"}
    title = (item.get("title") or "").lower()
    summary = (item.get("summary") or "").lower()
    text = title + " " + summary

    for keywords, region in _REGION_RULES:
        if any(kw in text for kw in keywords):
            item["region"] = region
            return item

    if item.get("region") not in valid:
        item["region"] = "Global"
    return item


# -- company dedupe ------------------------------------------------------------
def _dedup_company_news(items: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """
    同一 ticker + 同一 event_type 只保留 importance_score 最高的一条。
    特殊处理 management：同一 ticker 的 management 条目
    若 title 高度相似（共享超过3个连续词）则视为重复，只保留1条。
    """
    import re

    def _title_words(t: str) -> list[str]:
        return re.findall(r"[a-zA-Z]{3,}", (t or "").lower())

    def _is_similar(t1: str, t2: str, threshold: int = 4) -> bool:
        w1 = _title_words(t1)
        w2_set = set(_title_words(t2))
        common = sum(1 for w in w1 if w in w2_set)
        return common >= threshold

    sorted_items = sorted(
        items,
        key=lambda x: -(x.get("importance_score") or 0),
    )

    kept: list[dict[str, Any]] = []
    for item in sorted_items:
        ticker = (item.get("ticker") or "").strip().upper()
        event = (item.get("event_type") or "other").strip()
        title = item.get("title") or ""

        duplicate = False
        for k in kept:
            k_ticker = (k.get("ticker") or "").strip().upper()
            k_event = (k.get("event_type") or "other").strip()
            k_title = k.get("title") or ""

            if k_ticker != ticker:
                continue

            if event == k_event and event != "other":
                duplicate = True
                break

            if event in ("management", "other") and k_event in ("management", "other"):
                if _is_similar(title, k_title):
                    duplicate = True
                    break

        if not duplicate:
            kept.append(item)

    return kept


# -- source priority -----------------------------------------------------------
_SOURCE_PRIORITY: dict[str, int] = {
    "bloomberg": 10,
    "benzinga": 9,
    "reuters": 8,
    "ap ": 7,
    "wsj": 7,
    "ft ": 7,
    "finnhub-finnhub": 3,
    "finnhub-yahoo": 2,
    "finnhub-": 2,
}


def _source_score(source: str) -> int:
    s = (source or "").lower()
    for key, score in _SOURCE_PRIORITY.items():
        if key in s:
            return score
    return 5


def _dedup_by_source(items: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """
    同一 ticker + 同一 event_type 有多条时，
    优先保留来源分更高的（Bloomberg > Benzinga > Finnhub）。
    配合 _dedup_company_news 使用：先按来源排序，再去重。
    """
    return sorted(
        items,
        key=lambda x: (
            -(x.get("importance_score") or 0),
            -_source_score(x.get("source") or ""),
        ),
    )


def _is_useful(item: dict[str, Any]) -> bool:
    imp = item.get("importance_score") or 0
    summary = (item.get("summary") or "").strip()
    if imp <= 6 and ("原文信息不足" in summary or "原文信息有限" in summary):
        return False
    return True


def post_process_payload(payload: dict[str, Any], active_tickers: set[str]) -> dict[str, Any]:
    """
    LLM 返回 payload 后的代码层后处理：
    1. 过滤不在 active_tickers 内的公司新闻
    2. 修正 macro_news 的 region
    3. 公司新闻按来源优先级排序后去重
    4. 过滤 importance_score <= 5 且 summary 为"原文信息不足"的条目
    """
    macro_news = payload.get("macro_news") or []
    macro_news = [_fix_region(item) for item in macro_news if isinstance(item, dict)]

    company_news = payload.get("company_news") or []
    company_news = [item for item in company_news if isinstance(item, dict)]
    if active_tickers:
        company_news = [
            item
            for item in company_news
            if (item.get("ticker") or "").strip().upper() in active_tickers
        ]
    company_news = _dedup_by_source(company_news)
    company_news = _dedup_company_news(company_news)

    macro_news = [item for item in macro_news if _is_useful(item)]
    company_news = [item for item in company_news if _is_useful(item)]
    payload["macro_news"] = macro_news
    payload["company_news"] = company_news

    return payload

