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
