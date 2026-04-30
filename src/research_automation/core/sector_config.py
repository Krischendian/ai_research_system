"""Sector 特定配置：关注项等。"""
from __future__ import annotations

SECTOR_WATCH_ITEMS: dict[str, list[str]] = {
    "AI_Job_Replacement": [
        "AI replacing workers / automation replacing jobs",
        "layoffs and headcount reduction due to AI",
        "workforce restructuring / headcount guidance",
        "AI deployment ROI and monetization",
        "management commentary on AI strategy and hiring",
        "insider buy/sell",
        "earnings and revenue guidance",
        "new partnerships or contracts",
        "M&A activity",
        "share buyback",
    ],
    "Natural_Gas": [
        "production guidance and well completion plans",
        "natural gas price outlook / Henry Hub",
        "CAPEX guidance and changes vs prior quarter",
        "LNG export capacity and contracts",
        "pipeline and infrastructure updates",
        "management commentary on supply/demand",
        "earnings and revenue guidance",
        "insider buy/sell",
        "M&A activity",
        "share buyback",
        "EPA / energy policy impact",
    ],
    "Technology": [
        "AI product/service revenue contribution",
        "cloud business growth guidance",
        "CAPEX guidance and changes vs prior quarter",
        "new AI partnerships or enterprise contracts",
        "antitrust / regulatory developments",
        "chip / semiconductor supply chain",
        "management commentary on AI strategy",
        "earnings and revenue guidance",
        "insider buy/sell",
        "M&A activity",
        "share buyback",
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


def get_sector_watch_items_str(sector: str) -> str:
    """返回格式化的关注点列表字符串，供 prompt 使用。"""
    items = get_sector_watch_items(sector)
    return "\n".join(f"- {item}" for item in items)
