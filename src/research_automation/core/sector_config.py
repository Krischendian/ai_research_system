"""Sector 特定配置：关注项等。"""
from __future__ import annotations

SECTOR_WATCH_ITEMS: dict[str, list[str]] = {
    "AI_Job_Replacement": [
        "AI裁员态度与人员规划",
        "AI替代方向与落地进展",
        "数字化转型投入",
        "与上季度管理层指引变化",
    ],
    "Natural_Gas": [
        "产量指引与完井计划",
        "天然气价格预期",
        "资本支出计划（CAPEX guidance）",
        "与上季度管理层指引变化",
    ],
    "Technology": [
        "AI产品/服务收入贡献",
        "云业务增长指引",
        "资本支出计划",
        "与上季度管理层指引变化",
    ],
}

UNIVERSAL_WATCH_ITEMS: list[str] = [
    "管理层指引变化（与上季度对比）",
    "宏观环境影响",
]


def get_sector_watch_items(sector: str) -> list[str]:
    sec = (sector or "").strip()
    specific = SECTOR_WATCH_ITEMS.get(sec, [])
    return specific + UNIVERSAL_WATCH_ITEMS


# 每个sector关注的宏观关键词（用于过滤Bloomberg RSS宏观新闻）
SECTOR_MACRO_KEYWORDS: dict[str, list[str]] = {
    "AI_Job_Replacement": [
        "federal reserve", "interest rate", "fed ",
        "employment", "payroll", "unemployment", "labor market",
        "tariff", "trade", "outsourcing",
        "ai regulation", "artificial intelligence policy",
        "india it", "tech layoff", "workforce",
        "automation", "white collar",
    ],
    "Natural_Gas": [
        "natural gas", "lng", "opec", "energy price",
        "oil price", "pipeline", "henry hub",
        "federal reserve", "interest rate",
        "russia", "europe energy", "winter demand",
        "epa", "carbon", "climate policy",
    ],
    "Technology": [
        "federal reserve", "interest rate",
        "ai regulation", "antitrust", "chip", "semiconductor",
        "china tech", "export control", "tariff",
        "cloud", "cybersecurity", "data privacy",
        "nvidia", "microsoft", "alphabet",
    ],
}

# 通用宏观关键词（所有sector都关注）
UNIVERSAL_MACRO_KEYWORDS: list[str] = [
    "federal reserve", "fed rate", "interest rate",
    "inflation", "cpi", "gdp",
    "recession", "central bank",
    "geopolit", "sanctions", "war",
    "tariff", "trade war",
]


def get_sector_macro_keywords(sector: str) -> list[str]:
    """获取sector专属宏观关键词 + 通用关键词，去重。"""
    sec = (sector or "").strip()
    specific = SECTOR_MACRO_KEYWORDS.get(sec, [])
    all_kw = specific + UNIVERSAL_MACRO_KEYWORDS
    seen = set()
    result = []
    for kw in all_kw:
        if kw not in seen:
            seen.add(kw)
            result.append(kw)
    return result
