"""
chart_service.py — E-1 财务趋势图表
生成 Plotly 交互图：Revenue / Gross Margin % / CAPEX / EBITDA 时间序列
"""
from __future__ import annotations

from typing import Optional

import plotly.graph_objects as go
from plotly.subplots import make_subplots


# ── 颜色板（最多20家公司）──────────────────────────────────────────
_COLORS = [
    "#4C9BE8", "#E8834C", "#4CE87A", "#E84C6E", "#B04CE8",
    "#E8D44C", "#4CE8D4", "#E84CB0", "#8CE84C", "#4C6EE8",
    "#E8A84C", "#4CE8B0", "#E84C4C", "#4CB0E8", "#A8E84C",
    "#E84CE8", "#4CE86E", "#E8B04C", "#6EE84C", "#4CE8A8",
]


def _color(i: int) -> str:
    return _COLORS[i % len(_COLORS)]


def build_financial_trend_chart(
    financials_by_company: dict[str, list[dict]],
    sector_name: str = "",
) -> Optional[go.Figure]:
    """
    参数
    ----
    financials_by_company : {ticker: [AnnualFinancials-like dict, ...]}
        每个 dict 需含字段：
          year, revenue, gross_margin_pct, capex, ebitda
        按 year 升序排列（旧→新）

    返回 Plotly Figure（4行1列子图）
    """
    if not financials_by_company:
        return None

    companies = list(financials_by_company.keys())

    fig = make_subplots(
        rows=4, cols=1,
        shared_xaxes=True,
        subplot_titles=[
            "Revenue (USD M)",
            "Gross Margin %",
            "CAPEX (USD M)",
            "EBITDA (USD M)",
        ],
        vertical_spacing=0.07,
    )

    metrics = [
        ("revenue",          1, False),
        ("gross_margin_pct", 2, True),   # True = 百分比
        ("capex",            3, False),
        ("ebitda",           4, False),
    ]

    for idx, ticker in enumerate(companies):
        rows = financials_by_company[ticker]
        if not rows:
            continue
        years = [str(r.get("year", "")) for r in rows]
        color = _color(idx)

        for field, row_num, is_pct in metrics:
            values = []
            for r in rows:
                v = r.get(field)
                if v is None:
                    values.append(None)
                else:
                    # revenue/capex/ebitda 转为百万
                    if not is_pct and v is not None:
                        values.append(round(v / 1_000_000, 1))
                    else:
                        values.append(round(v, 2) if v is not None else None)

            fig.add_trace(
                go.Scatter(
                    x=years,
                    y=values,
                    mode="lines+markers",
                    name=ticker,
                    legendgroup=ticker,
                    showlegend=(row_num == 1),   # 只在第一个子图显示图例
                    line=dict(color=color, width=2),
                    marker=dict(size=6),
                    hovertemplate=(
                        f"<b>{ticker}</b><br>"
                        "Year: %{x}<br>"
                        "Value: %{y}" + ("%" if is_pct else "M") + "<extra></extra>"
                    ),
                ),
                row=row_num, col=1,
            )

    title = f"{sector_name} — Financial Trends" if sector_name else "Financial Trends"
    fig.update_layout(
        title=dict(text=title, font=dict(size=18)),
        height=900,
        hovermode="x unified",
        legend=dict(
            orientation="h",
            yanchor="bottom",
            y=1.02,
            xanchor="right",
            x=1,
        ),
        plot_bgcolor="rgba(0,0,0,0)",
        paper_bgcolor="rgba(0,0,0,0)",
        font=dict(color="#e0e0e0"),
        margin=dict(t=100, b=40, l=60, r=20),
    )
    fig.update_xaxes(showgrid=True, gridcolor="rgba(255,255,255,0.1)")
    fig.update_yaxes(showgrid=True, gridcolor="rgba(255,255,255,0.1)")

    return fig


def build_single_company_chart(
    ticker: str,
    rows: list[dict],
) -> Optional[go.Figure]:
    """单公司柱状图，用于 DeepDive 页面（可选）"""
    if not rows:
        return None
    return build_financial_trend_chart({ticker: rows})
