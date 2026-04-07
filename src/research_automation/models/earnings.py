"""财报电话会分析数据结构。"""
from pydantic import BaseModel, Field


class EarningsQuotation(BaseModel):
    """电话会中的可引用原话。"""

    speaker: str = ""
    quote: str
    topic: str = ""  # 如 Guidance、China、AI
    source_paragraph_ids: list[str] = Field(
        default_factory=list,
        description="逐字稿分段 ID",
    )


class EarningsViewpoint(BaseModel):
    """一条带溯源的观点或要点。"""

    text: str
    source_paragraph_ids: list[str] = Field(default_factory=list)


class EarningsCallAnalysis(BaseModel):
    """LLM 对单季度财报电话会的分析结果。"""

    ticker: str
    quarter: str  # 如 2024Q4
    summary: str = ""  # 中文概括
    summary_source_paragraph_ids: list[str] = Field(
        default_factory=list,
        description="摘要所依据的逐字稿段落 ID",
    )
    management_viewpoints: list[EarningsViewpoint] = Field(default_factory=list)
    quotations: list[EarningsQuotation] = Field(default_factory=list)
    new_business_highlights: list[EarningsViewpoint] = Field(
        default_factory=list,
        description="新业务 / 产品线 / 战略动向要点",
    )
    last_updated: str = ""
    data_source_label: str = ""
    document_uid: str = Field(
        "",
        description="逐字稿文档键，与 document_paragraphs.doc_uid 一致",
    )
    source_paragraphs: dict[str, str] = Field(
        default_factory=dict,
        description="本响应涉及的段落 ID → 原文",
    )
