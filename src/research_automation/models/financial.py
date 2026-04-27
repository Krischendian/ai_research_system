"""财务指标 Pydantic 模型（数据契约）。"""
from typing import Optional

from pydantic import BaseModel, computed_field


class AnnualFinancials(BaseModel):
    """单年财务数据。"""

    year: int  # 会计年度，如 2023
    revenue: Optional[float] = None  # 营收（美元）
    ebitda: Optional[float] = None  # EBITDA（美元）
    capex: Optional[float] = None  # 资本支出（美元）
    gross_margin: Optional[float] = None  # 毛利率，小数形式，如 0.441 表示 44.1%
    net_debt_to_equity: Optional[float] = None  # 净负债/权益比
    net_income: Optional[float] = None  # 净利润（美元）


class CompanyFinancials(BaseModel):
    """公司完整财务数据。"""

    ticker: str  # 股票代码，如 AAPL
    financials: list[AnnualFinancials]  # 各年度财务数据列表（通常含最近三年）
    last_updated: str  # 本条数据最后更新时间（建议 ISO8601 字符串）
    # 主数据源标识：如 SEC EDGAR；无有效行时为 None
    data_source: str | None = None
    data_source_label: str = ""  # 数据溯源说明（人读）
    primary_source_url: str | None = None  # 对外原始数据入口（如 SEC 检索）


class QuarterlyFinancials(BaseModel):
    """单季度财务数据"""

    ticker: str
    year: int
    period: str  # "Q1" / "Q2" / "Q3" / "Q4"
    quarter_label: str  # "2024Q1" 供显示用
    date: str  # "2024-12-28"
    revenue: Optional[float] = None
    gross_profit: Optional[float] = None
    gross_margin: Optional[float] = None  # 小数形式
    net_income: Optional[float] = None
    ebitda: Optional[float] = None
    capex: Optional[float] = None  # 负数，原始值

    @computed_field  # 与 dict 数据源 ``quarter`` 键及图表对齐
    @property
    def quarter(self) -> str:
        return self.quarter_label
