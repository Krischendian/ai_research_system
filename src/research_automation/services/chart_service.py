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
        text=[f"{v:+.1f}%" if v >= 0 else f"{v:.1f}%" for v in values],
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
 
 
def build_quarterly_sector_charts(
    quarterly_data: dict[str, list],
    sector_name: str = "",
) -> list[go.Figure]:
    """
    生成板块汇总季度图表（6个图）。
    quarterly_data: {ticker: [QuarterlyFinancials, ...]}
    返回 [fig1, fig2, fig3, fig4, fig5, fig6]
    """
    import pandas as pd
 
    # 汇总所有公司数据到同一个季度
    quarters: list[str] = []
    for rows in quarterly_data.values():
        for r in rows:
            ql = (
                r.quarter
                if hasattr(r, "quarter")
                else r.get("quarter", "")
            )
            if ql and ql not in quarters:
                quarters.append(ql)
    quarters = sorted(set(quarters))
 
    def _sum_field(field: str, quarters: list[str]) -> list[float | None]:
        vals = []
        for ql in quarters:
            total = 0.0
            has = False
            for rows in quarterly_data.values():
                for r in rows:
                    ql_r = (
                        r.quarter
                        if hasattr(r, "quarter")
                        else r.get("quarter", "")
                    )
                    if ql_r == ql:
                        v = getattr(r, field, None) if hasattr(r, field) else r.get(field)
                        if v is not None:
                            total += float(v)
                            has = True
            vals.append(total if has else None)
        return vals

    def _avg_field(field: str, quarters: list[str]) -> list[float | None]:
        vals = []
        for ql in quarters:
            items = []
            for rows in quarterly_data.values():
                for r in rows:
                    ql_r = (
                        r.quarter
                        if hasattr(r, "quarter")
                        else r.get("quarter", "")
                    )
                    if ql_r == ql:
                        v = getattr(r, field, None) if hasattr(r, field) else r.get(field)
                        if v is not None:
                            items.append(float(v))
            vals.append(statistics.mean(items) if items else None)
        return vals
 
    revenues = _sum_field("revenue", quarters)
    capexes = [abs(v) if v is not None else None for v in _sum_field("capex", quarters)]
    gross_margins = [v * 100 if v is not None else None for v in _avg_field("gross_margin", quarters)]
 
    # ── 共用布局基础 ─────────────────────────────────────────
    _base = dict(
        plot_bgcolor="rgba(0,0,0,0)",
        paper_bgcolor="rgba(0,0,0,0)",
        font=dict(color="#e0e0e0", size=11),
        height=320,
        margin=dict(t=50, b=60, l=60, r=20),
    )
    _xaxis = dict(title="Quarter", tickangle=-45, showgrid=True,
                  gridcolor="rgba(255,255,255,0.08)", tickfont=dict(size=10))
    _yaxis = dict(showgrid=True, gridcolor="rgba(255,255,255,0.08)", tickfont=dict(size=10))
 
    # ── 先算 CAPEX 2-Period ROC（多图共用）─────────────────────
    capex_roc: list[float | None] = [None, None]
    for i in range(2, len(capexes)):
        if capexes[i] and capexes[i - 2] and capexes[i - 2] != 0:
            capex_roc.append(round((capexes[i] - capexes[i - 2]) / abs(capexes[i - 2]), 4))
        else:
            capex_roc.append(None)
 
    figs = []
 
    # ── 图1（左上）：CAPEX 2-Period Rate of Change 折线 ──────
    fig1 = go.Figure(go.Scatter(
        x=quarters, y=capex_roc,
        mode="lines+markers",
        line=dict(color="#F4A460", width=2),
        marker=dict(size=6),
        hovertemplate="<b>%{x}</b><br>CAPEX ROC: %{y:.3f}<extra></extra>",
    ))
    fig1.update_layout(
        title=dict(text=f"2-Period Rate of Change of CAPEX (Quarterly)", font=dict(size=12)),
        xaxis={**_xaxis},
        yaxis={**_yaxis, "title": "Rate of Change"},
        **_base,
    )
    figs.append(fig1)
 
    # ── 图2（右上）：Total CAPEX per Quarter 柱状（正值）────────
    fig2 = go.Figure(go.Bar(
        x=quarters,
        y=[v / 1e9 if v is not None else None for v in capexes],
        marker_color="#4C9BE8",
        hovertemplate="<b>%{x}</b><br>CAPEX: $%{y:.2f}B<extra></extra>",
    ))
    fig2.update_layout(
        title=dict(text="Total Capital Expenditures Per Quarter (All Stocks, Flipped Positive)", font=dict(size=12)),
        xaxis={**_xaxis},
        yaxis={**_yaxis, "title": "CAPEX (USD B)"},
        **_base,
    )
    figs.append(fig2)
 
    # ── 图3（左中）：Gross Margin vs CAPEX ROC 双轴叠加 ─────────
    fig3 = go.Figure()
    fig3.add_trace(go.Scatter(
        x=quarters, y=gross_margins,
        name="Gross Margin %", yaxis="y1",
        mode="lines+markers",
        line=dict(color="#4C9BE8", width=2),
        marker=dict(size=6),
        hovertemplate="<b>%{x}</b><br>Gross Margin: %{y:.1f}%<extra></extra>",
    ))
    fig3.add_trace(go.Scatter(
        x=quarters, y=capex_roc,
        name="CAPEX 2P ROC", yaxis="y2",
        mode="lines+markers",
        line=dict(color="#E84C6E", width=1.5, dash="dot"),
        marker=dict(size=5),
        hovertemplate="<b>%{x}</b><br>CAPEX ROC: %{y:.3f}<extra></extra>",
    ))
    fig3.update_layout(
        title=dict(text="Overlay: Gross Margin vs CAPEX 2-Period ROC", font=dict(size=12)),
        xaxis={**_xaxis},
        yaxis={**_yaxis, "title": "Gross Margin %", "side": "left"},
        yaxis2={**_yaxis, "title": "CAPEX 2P ROC", "side": "right",
                "overlaying": "y", "showgrid": False},
        legend=dict(orientation="h", y=1.08, x=0, font=dict(size=10)),
        **_base,
    )
    figs.append(fig3)
 
    # ── 图4（右中）：Gross Margin 季度均值折线 ───────────────────
    # 动态 Y 轴范围：最小值-5% 到 最大值+5%，避免线太平
    gm_valid = [v for v in gross_margins if v is not None]
    if gm_valid:
        gm_min = max(0, min(gm_valid) - 5)
        gm_max = min(100, max(gm_valid) + 5)
    else:
        gm_min, gm_max = 0, 100
 
    fig4 = go.Figure(go.Scatter(
        x=quarters, y=gross_margins,
        mode="lines+markers",
        line=dict(color="#F4C430", width=2),
        marker=dict(size=6),
        hovertemplate="<b>%{x}</b><br>Gross Margin: %{y:.1f}%<extra></extra>",
    ))
    fig4.update_layout(
        title=dict(text="Gross Margin (Quarterly Average %)", font=dict(size=12)),
        xaxis={**_xaxis},
        yaxis={**_yaxis, "title": "Gross Margin %", "range": [gm_min, gm_max]},
        **_base,
    )
    figs.append(fig4)
 
    # ── 图5（左下）：Total Revenue per Quarter 柱状 ──────────────
    fig5 = go.Figure(go.Bar(
        x=quarters,
        y=[v / 1e9 if v is not None else None for v in revenues],
        marker_color="#4CE87A",
        hovertemplate="<b>%{x}</b><br>Revenue: $%{y:.2f}B<extra></extra>",
    ))
    fig5.update_layout(
        title=dict(text="Total Revenue Per Quarter (All Stocks)", font=dict(size=12)),
        xaxis={**_xaxis},
        yaxis={**_yaxis, "title": "Revenue (USD B)"},
        **_base,
    )
    figs.append(fig5)
 
    # ── 图6（右下）：CAPEX vs Revenue 双轴折线 ───────────────────
    fig6 = go.Figure()
    fig6.add_trace(go.Scatter(
        x=quarters,
        y=[(-v / 1e9) if v is not None else None for v in capexes],  # 负值显示（与截图一致）
        name="CAPEX (B, negative)", yaxis="y1",
        mode="lines+markers",
        line=dict(color="#4C9BE8", width=2),
        marker=dict(size=6),
        hovertemplate="<b>%{x}</b><br>CAPEX: -$%{y:.2f}B<extra></extra>",
    ))
    fig6.add_trace(go.Scatter(
        x=quarters,
        y=[v / 1e9 if v is not None else None for v in revenues],
        name="Revenue (B)", yaxis="y2",
        mode="lines+markers",
        line=dict(color="#4CE87A", width=2),
        marker=dict(size=6),
        hovertemplate="<b>%{x}</b><br>Revenue: $%{y:.2f}B<extra></extra>",
    ))
    fig6.update_layout(
        title=dict(text="Overlay: CAPEX vs Revenue (Dual Axis Line Chart)", font=dict(size=12)),
        xaxis={**_xaxis},
        yaxis={**_yaxis, "title": "CAPEX (USD B)", "side": "left"},
        yaxis2={**_yaxis, "title": "Revenue (USD B)", "side": "right",
                "overlaying": "y", "showgrid": False},
        legend=dict(orientation="h", y=1.08, x=0, font=dict(size=10)),
        **_base,
    )
    figs.append(fig6)
 
    return figs
 
 
def build_quarterly_single_company_charts(
    ticker: str,
    rows: list,
) -> list[go.Figure]:
    """单公司6图套装，逻辑与板块汇总相同但只用一家公司数据。"""
    return build_quarterly_sector_charts({ticker: rows}, sector_name=ticker)