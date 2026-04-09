"""晨报 / 新闻相关模型。"""
from typing import Literal, Optional

from pydantic import BaseModel, Field, field_validator

NewsSentiment = Literal["positive", "negative", "neutral"]


class NewsItem(BaseModel):
    """单条新闻。"""

    title: str  # 标题
    summary: str  # 摘要
    source: str  # 来源（如 RSS-Reuters）
    source_url: Optional[str] = None  # 原文链接（可点击溯源）
    # 发布时间（UTC ISO8601 或带 Z）；Finnhub 优先 Unix 换算
    published_at: Optional[str] = None
    # 智能洞察：1-10，缺省表示未参与评分或未返回
    importance_score: Optional[int] = Field(
        None,
        ge=1,
        le=10,
        description="重要性评分（聚类模型）",
    )
    matched_tickers: list[str] = Field(
        default_factory=list,
        description="从标题/摘要等文本中匹配到的监控池 ticker（大写）",
    )
    sentiment: Optional[NewsSentiment] = Field(
        None,
        description="摘要 LLM 给出的短期解读倾向；缺省由前端关键词回退",
    )

    @field_validator("source_url", "published_at", mode="before")
    @classmethod
    def empty_str_to_none(cls, v: object) -> object:
        if v == "":
            return None
        return v

    @field_validator("sentiment", mode="before")
    @classmethod
    def normalize_sentiment(cls, v: object) -> object:
        if v is None or v == "":
            return None
        if isinstance(v, str):
            s = v.strip().lower()
            if s in ("positive", "negative", "neutral"):
                return s
        return None


class ClusterNewsItem(BaseModel):
    """聚类内单条新闻（含评分，用于聚合展示/折叠信源）。"""

    title: str
    summary: str = ""
    source: str = ""
    source_url: Optional[str] = None
    published_at: Optional[str] = None
    matched_tickers: list[str] = Field(default_factory=list)
    importance_score: int = Field(5, ge=1, le=10)
    sentiment: Optional[NewsSentiment] = Field(
        None,
        description="与晨报摘要同源的情绪标签；缺省由前端回退",
    )

    @field_validator("sentiment", mode="before")
    @classmethod
    def normalize_cluster_sentiment(cls, v: object) -> object:
        if v is None or v == "":
            return None
        if isinstance(v, str):
            s = v.strip().lower()
            if s in ("positive", "negative", "neutral"):
                return s
        return None


class NewsCluster(BaseModel):
    """同一主题聚类（前端可展示一条代表题，下方折叠多信源）。"""

    cluster_id: str
    representative_title: str
    importance_score: int = Field(5, ge=1, le=10)
    news_items: list[ClusterNewsItem] = Field(default_factory=list)


class MorningBrief(BaseModel):
    """晨报响应：宏观 + 公司。"""

    macro_news: list[NewsItem]  # 宏观新闻
    company_news: list[NewsItem]  # 公司新闻
    data_source_label: str = ""  # 整体数据来源说明
    provenance_note: str = ""  # 使用提示（摘要须对照原文）
    clusters: list[NewsCluster] = Field(
        default_factory=list,
        description="聚类后的新闻组（合并重复主题）",
    )
    top_news: list[ClusterNewsItem] = Field(
        default_factory=list,
        description="重要性≥7 的条目（扁平列表）",
    )
    analyst_briefing: str = Field(
        default="",
        description="分析师早评（约 200 字内）",
    )


class OvernightNewsItem(BaseModel):
    """隔夜速递单条（RSS 提要 + 元数据）。"""

    title: str
    summary: str  # 英文提要/摘录
    source: str
    source_url: Optional[str] = None
    published_at_ny: Optional[str] = Field(
        None, description="America/New_York 本地 ISO8601"
    )
    importance_score: Optional[int] = Field(
        None,
        ge=1,
        le=10,
        description="重要性评分（聚类模型）",
    )
    matched_tickers: list[str] = Field(
        default_factory=list,
        description="监控池命中的 ticker（大写）",
    )

    @field_validator("source_url", mode="before")
    @classmethod
    def empty_str_to_none_overnight(cls, v: object) -> object:
        if v == "":
            return None
        return v


class OvernightNewsResponse(BaseModel):
    """隔夜速递 API 响应。"""

    summary: str  # 一句中文，通常以「隔夜重点关注：」开头
    news_list: list[OvernightNewsItem]
    window_start_ny: str = ""  # 纽约窗口起点 ISO
    window_end_ny: str = ""  # 纽约窗口终点 ISO
    provenance_note: str = ""
    clusters: list[NewsCluster] = Field(default_factory=list)
    top_news: list[ClusterNewsItem] = Field(default_factory=list)
    analyst_briefing: str = ""


class YesterdayThemeGroup(BaseModel):
    """昨日总结中的一条主题行（宏观或公司）。"""

    topic: str  # 中文主题短语
    count: int  # 归入该主题的新闻条数
    article_indices: list[int] = Field(
        default_factory=list,
        description="对应输入编号列表（1-based，与 LLM 提示一致）",
    )
    tickers: list[str] = Field(
        default_factory=list,
        description="公司类可选：涉及 ticker（大写）",
    )


class YesterdaySummaryResponse(BaseModel):
    """昨日总结 API：分类汇总（Markdown + 结构化）。"""

    markdown: str
    macro: list[YesterdayThemeGroup]
    company: list[YesterdayThemeGroup]
    articles_in_window: int = 0
    window_start_ny: str = ""
    window_end_ny: str = ""
    provenance_note: str = ""
    clusters: list[NewsCluster] = Field(default_factory=list)
    top_news: list[ClusterNewsItem] = Field(default_factory=list)
    analyst_briefing: str = ""
