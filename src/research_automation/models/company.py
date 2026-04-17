"""公司 / 业务画像 Pydantic 模型（数据契约）。"""
from typing import Optional

from pydantic import BaseModel, Field, field_validator


class CorporateAction(BaseModel):
    """公司近期结构化动态（须可溯源至原文；action_type 为固定枚举字符串）。"""

    action_type: str  # "new_business" | "acquisition" | "partnership"
    description: str  # 基于原文的简短说明（中文或节选语言，勿臆测）
    date: Optional[str] = None  # 原文明确日期，无则 None
    source_quote: str  # 披露原文逐字连续片段，供核对
    source_paragraph_ids: list[str] = Field(
        default_factory=list,
        description="对应 document_paragraphs 段落 ID",
    )
    source_url: Optional[str] = Field(
        None,
        description="新闻类动态的外链原文；10-K 抽取条目通常为空",
    )

    @field_validator("source_url", mode="before")
    @classmethod
    def _empty_source_url_to_none(cls, v: object) -> object:
        if v == "":
            return None
        return v


class KeyManagementQuote(BaseModel):
    """披露原文中的关键管理层原话（须为逐字摘录，见 profile_service 抽取规则）。"""

    speaker: str  # 原文明确标注的身份/姓名，无法确认则为 "UNKNOWN"
    quote: str  # 与原文完全一致的连续片段，禁止翻译或改写
    topic: str  # 简短英文标签（约 1–3 词），如 Guidance, China
    source_paragraph_ids: list[str] = Field(
        default_factory=list,
        description="溯源段落 ID",
    )
    modality: str = Field(
        "fact",
        description=(
            "'fact' | 'forward_looking' | 'uncertain'。"
            "'forward_looking' 表示原文含 expect/plan/may/consider/intend/target 等情态词；"
            "'uncertain' 表示原文含 may/might/could 等不确定词；"
            "'fact' 表示已发生的确定性陈述。"
            "由 hallucination_guard 校验，禁止将 forward_looking 原文归类为 fact。"
        ),
    )
    data_source: Optional[str] = Field(
        None,
        description='来源：`earnings_call` 为电话会分析复用；缺省为 10-K 画像抽取',
    )
    source_url: Optional[str] = Field(
        None,
        description="电话会条目跳转深度分析页的 URL（含 ticker、quarter）",
    )

    @field_validator("data_source", "source_url", mode="before")
    @classmethod
    def _empty_str_to_none(cls, v: object) -> object:
        if v == "":
            return None
        return v


class SegmentMix(BaseModel):
    """业务线或地区收入占比。"""

    segment_name: str  # 分业务线或分地区的名称，如 iPhone、Americas
    percentage: str  # 占比字符串，必须包含百分号，如 "45.2%"
    source_paragraph_ids: list[str] = Field(
        default_factory=list,
        description="溯源段落 ID",
    )

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
    # 均须来自披露原文的可验证摘录语义；无明确表述时用占位，禁止模型臆测（见 profile_service Prompt）
    future_guidance: str = "原文未明确提及"  # 公司对未来展望（如指引、下季度表述）
    industry_view: str = "原文未明确提及"  # 管理层对行业状态的看法（须为原文明确表述）
    industry_view_source: Optional[str] = Field(
        None,
        description="行业判断对应的原文摘录或段落引用（供前端弹窗核对；无则 None）",
    )
    key_quotes: list[KeyManagementQuote] = Field(
        default_factory=list,
        description="关键管理层原话（逐字摘录，无则空列表）",
    )
    corporate_actions: list[CorporateAction] = Field(
        default_factory=list,
        description="近期公司动态：新业务 / 收购 / 合作（须含原文引用）",
    )
    last_updated: str  # 本条画像最后更新时间（建议 ISO8601 字符串）
    data_source_label: str = ""  # 数据溯源（示例节选 + LLM 等）
    primary_source_url: Optional[str] = None  # 法定披露检索入口（如 EDGAR）
    document_uid: Optional[str] = Field(
        None,
        description="10-K 节选文档唯一键（Item1+ 多章节合并时与 document_paragraphs.doc_uid 一致）",
    )
    field_paragraph_ids: dict[str, list[str]] = Field(
        default_factory=dict,
        description="顶层文案字段 → 段落 ID 列表（如 core_business、future_guidance）",
    )
    source_paragraphs: dict[str, str] = Field(
        default_factory=dict,
        description="本响应涉及的段落 ID → 原文全文",
    )
    validation_warning: Optional[str] = Field(
        None,
        description="与 FMP 业务线营收基准比对后的提示；偏差超阈值时提醒人工复核",
    )
