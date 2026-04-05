"""晨报 / 新闻相关模型。"""
from typing import Optional

from pydantic import BaseModel, field_validator


class NewsItem(BaseModel):
    """单条新闻。"""

    title: str  # 标题
    summary: str  # 摘要
    source: str  # 来源（如 RSS-Reuters）
    source_url: Optional[str] = None  # 原文链接（可点击溯源）

    @field_validator("source_url", mode="before")
    @classmethod
    def empty_str_to_none(cls, v: object) -> object:
        if v == "":
            return None
        return v


class MorningBrief(BaseModel):
    """晨报响应：宏观 + 公司。"""

    macro_news: list[NewsItem]  # 宏观新闻
    company_news: list[NewsItem]  # 公司新闻
    data_source_label: str = ""  # 整体数据来源说明
    provenance_note: str = ""  # 使用提示（摘要须对照原文）
