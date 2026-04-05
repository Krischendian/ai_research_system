"""公司/业务画像 Pydantic 模型（与 project_plan 数据契约一致）。"""
from typing import List

from pydantic import BaseModel, field_validator


class SegmentMix(BaseModel):
    """业务线或地区收入占比"""

    segment_name: str
    percentage: str

    @field_validator("percentage")
    @classmethod
    def must_have_percent(cls, v: str) -> str:
        if "%" not in v:
            raise ValueError(f"Percentage must include %, got {v}")
        return v


class BusinessProfile(BaseModel):
    """公司业务画像"""

    ticker: str
    core_business: str
    revenue_by_segment: List[SegmentMix]
    revenue_by_geography: List[SegmentMix]
    last_updated: str
