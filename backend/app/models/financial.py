"""财务指标 Pydantic 模型（与 project_plan 数据契约一致）。"""
from typing import Optional

from pydantic import BaseModel


class AnnualFinancials(BaseModel):
    """单年财务数据"""

    fiscal_year: int
    revenue: Optional[float] = None
    ebitda: Optional[float] = None
    capex: Optional[float] = None
    gross_margin: Optional[float] = None
    net_debt_to_equity: Optional[float] = None


class CompanyFinancials(BaseModel):
    """公司完整财务数据"""

    ticker: str
    company_name: Optional[str] = None
    financials: list[AnnualFinancials]
    last_updated: str
