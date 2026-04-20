"""
chart_service.py — 财务图表
图1：Sector 分位趋势图（Top25% / 中位数 / Bottom25%）
图2：个股 Revenue YoY 横向排名图
"""
from __future__ import annotations

import statistics
from typing import Optional

import plotly.graph_objects as go
from plotly.subplots import make_subplots


def build_financial_trend_chart(
    financials_by_company: dict[str, list[dict]],
    sector_name: str = "",
) -> Optional[go.Figure]:
    """
    图1：Sector 分位趋势图
    每个指标只显示3条线：Top25% / 中位数 / Bottom25%
    让分析师看清 sector 整体趋势和内部分化程度。
    """
    if not financials_by_company:
        return None

    # 收集所有年份
    all_years: set[int] = set()
    for rows in financials_by_company.values():
        for r in rows:
            y = r.get("year")
            if y:
                all_years.add(int(y))
    years = sorted(all_years)
    if not years:
        return None

    metrics = [
        ("revenue",          "Revenue (USD B)",    1, 1_000_000_000),
        ("gross_margin_pct", "Gross Margin %",     2, 1),
        ("capex",            "CAPEX (USD B)",      3, 1_000_000_000),
        ("ebitda",           "EBITDA (USD B)",     4, 1_000_000_000),
    ]

    fig = make_subplots(
        rows=4, cols=1,
        shared_xaxes=True,
        subplot_titles=[m[1] for m in metrics],
        vertical_spacing=0.08,
    )

    colors = {
        "top":    "#4C9BE8",   # 蓝
        "median": "#4CE87A",   # 绿
        "bottom": "#E84C6E",   # 红
    }

    for field, label, row_num, divisor in metrics:
        top_vals, med_vals, bot_vals = [], [], []

        for y in years:
            vals = []
            for rows in financials_by_company.values():
                for r in rows:
                    if int(r.get("year", 0)) == y:
                        v = r.get(field)
                        if v is not None:
                            # gross_margin_pct 异常值过滤
                            if field == "gross_margin_pct" and (v > 95 or v < -50):
                                break
                            vals.append(float(v) / divisor)
            if len(vals) >= 2:
                vals_sorted = sorted(vals)
                n = len(vals_sorted)
                top_vals.append(round(vals_sorted[int(n * 0.75)], 2))
                med_vals.append(round(statistics.median(vals_sorted), 2))
                bot_vals.append(round(vals_sorted[int(n * 0.25)], 2))
            else:
                top_vals.append(None)
                med_vals.append(None)
                bot_vals.append(None)

        year_strs = [str(y) for y in years]
        suffix = "%" if field == "gross_margin_pct" else "B"

        for name, vals, color, dash in [
            ("Top 25%",  top_vals,  colors["top"],    "dot"),
            ("中位数",    med_vals,  colors["median"], "solid"),
            ("Bottom 25%", bot_vals, colors["bottom"], "dot"),
        ]:
            fig.add_trace(
                go.Scatter(
                    x=year_strs,
                    y=vals,
                    mode="lines+markers",
                    name=name,
                    legendgroup=name,
                    showlegend=(row_num == 1),
                    line=dict(color=color, width=2, dash=dash),
                    marker=dict(size=7),
                    hovertemplate=f"<b>{name}</b><br>Year: %{{x}}<br>{label}: %{{y}}{suffix}<extra></extra>",
                ),
                row=row_num, col=1,
            )

    title = f"{sector_name} — Sector 分位趋势" if sector_name else "Sector 分位趋势"
    fig.update_layout(
        title=dict(text=title, font=dict(size=16)),
        height=900,
        hovermode="x unified",
        legend=dict(orientation="h", yanchor="bottom", y=1.02, xanchor="right", x=1),
        plot_bgcolor="rgba(0,0,0,0)",
        paper_bgcolor="rgba(0,0,0,0)",
        font=dict(color="#e0e0e0"),
        margin=dict(t=100, b=40, l=60, r=20),
    )
    fig.update_xaxes(showgrid=True, gridcolor="rgba(255,255,255,0.1)")
    fig.update_yaxes(showgrid=True, gridcolor="rgba(255,255,255,0.1)")

    return fig


def build_yoy_ranking_chart(
    financials_by_company: dict[str, list[dict]],
    sector_name: str = "",
) -> Optional[go.Figure]:
    """
    图2：个股 Revenue YoY 横向排名图
    按最新年度 YoY 排序，正增长绿色，负增长红色。
    """
    if not financials_by_company:
        return None

    yoy_data: list[tuple[str, float]] = []

    for ticker, rows in financials_by_company.items():
        if len(rows) < 2:
            continue
        rows_sorted = sorted(rows, key=lambda r: r.get("year", 0))
        latest = rows_sorted[-1]
        prev = rows_sorted[-2]
        rev_new = latest.get("revenue")
        rev_old = prev.get("revenue")
        if rev_new and rev_old and rev_old != 0:
            yoy = (float(rev_new) - float(rev_old)) / abs(float(rev_old)) * 100
            yoy_data.append((ticker, round(yoy, 1)))

    if not yoy_data:
        return None

    yoy_data.sort(key=lambda x: x[1])
    tickers = [d[0] for d in yoy_data]
    values = [d[1] for d in yoy_data]
    colors = ["#4CE87A" if v >= 0 else "#E84C6E" for v in values]

    fig = go.Figure(go.Bar(
        x=values,
        y=tickers,
        orientation="h",
        marker_color=colors,
        text=[f"{v:+.1f}%" for v in values],
        textposition="outside",
        hovertemplate="<b>%{y}</b><br>Revenue YoY: %{x:.1f}%<extra></extra>",
    ))

    title = f"{sector_name} — Revenue YoY 排名" if sector_name else "Revenue YoY 排名"
    fig.update_layout(
        title=dict(text=title, font=dict(size=16)),
        height=max(300, len(tickers) * 35),
        xaxis=dict(title="Revenue YoY %", zeroline=True, zerolinecolor="white"),
        plot_bgcolor="rgba(0,0,0,0)",
        paper_bgcolor="rgba(0,0,0,0)",
        font=dict(color="#e0e0e0"),
        margin=dict(t=60, b=40, l=80, r=60),
    )

    return fig


def build_single_company_chart(
    ticker: str,
    rows: list[dict],
) -> Optional[go.Figure]:
    """单公司趋势图，用于 DeepDive 页面"""
    if not rows:
        return None
    return build_financial_trend_chart({ticker: rows})
