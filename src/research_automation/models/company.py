"""公司 / 业务画像 Pydantic 模型（数据契约）。"""
from typing import Optional

from pydantic import BaseModel, field_validator


class SegmentMix(BaseModel):
    """业务线或地区收入占比。"""

    segment_name: str  # 分业务线或分地区的名称，如 iPhone、Americas
    percentage: str  # 占比字符串，必须包含百分号，如 "45.2%"

    @field_validator("percentage")
    @classmethod
    def must_have_percent(cls, v: str) -> str:
        if "%" not in v:
            raise ValueError(f"占比须包含 %，当前为: {v}")
        return v


class BusinessProfile(BaseModel):
    """公司业务画像。"""

    ticker: str  # 股票代码
    core_business: str  # 核心业务与经营描述
    revenue_by_segment: list[SegmentMix]  # 按业务线拆分的收入占比列表
    revenue_by_geography: list[SegmentMix]  # 按地区拆分的收入占比列表
    last_updated: str  # 本条画像最后更新时间（建议 ISO8601 字符串）
    data_source_label: str = ""  # 数据溯源（示例节选 + LLM 等）
    primary_source_url: Optional[str] = None  # 法定披露检索入口（如 EDGAR）
