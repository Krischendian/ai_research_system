"""
Sector 矩阵总览 — E-2
一张表扫完整个 sector 所有公司的关键财务指标。
零 LLM 调用，纯 FMP 数据，秒级加载。
"""
from __future__ import annotations

import sys
from pathlib import Path

_fe_root = Path(__file__).resolve().parent.parent.parent
_src = _fe_root / "src"
for p in (_fe_root, _src):
    if str(p) not in sys.path:
        sys.path.insert(0, str(p))

import streamlit as st
import pandas as pd
from dotenv import load_dotenv

from research_automation.core.database import get_connection, init_db
from research_automation.extractors.fmp_client import FMPClient

load_dotenv(_fe_root / ".env", override=False)

st.set_page_config(page_title="Sector 矩阵", layout="wide")
st.title("📊 Sector 矩阵总览")
st.caption("所有公司关键财务指标一览，数据来源：FMP Annual Financials")


# ── 工具函数 ──────────────────────────────────────────────────────

def _distinct_sectors() -> list[str]:
    conn = get_connection()
    try:
        init_db(conn)
        cur = conn.execute(
            "SELECT DISTINCT sector FROM companies "
            "WHERE is_active=1 AND TRIM(sector)!='' ORDER BY sector"
        )
        return [str(r[0]).strip() for r in cur.fetchall() if r[0]]
    finally:
        conn.close()


def _get_tickers(sector: str) -> list[tuple[str, str]]:
    """返回 [(ticker, company_name), ...]"""
    conn = get_connection()
    try:
        init_db(conn)
        cur = conn.execute(
            "SELECT ticker, company_name FROM companies "
            "WHERE is_active=1 AND TRIM(sector)=? ORDER BY ticker",
            (sector,),
        )
        return [(r[0], r[1] or r[0]) for r in cur.fetchall()]
    finally:
        conn.close()


def _fmt_b(v: float | None) -> str:
    if v is None:
        return "—"
    x = abs(float(v))
    if x >= 1e9:
        return f"${float(v)/1e9:.1f}B"
    if x >= 1e6:
        return f"${float(v)/1e6:.0f}M"
    return f"${float(v):,.0f}"


def _fmt_pct(v: float | None) -> str:
    if v is None:
        return "—"
    # 兼容小数和百分比两种格式
    val = float(v)
    if val < 2:
        val *= 100
    return f"{val:.1f}%"


def _fmt_x(v: float | None) -> str:
    if v is None:
        return "—"
    return f"{float(v):.2f}x"


def _load_matrix(sector: str) -> pd.DataFrame:
    """拉取所有公司最新一年财务数据，组装矩阵DataFrame。"""
    tickers = _get_tickers(sector)
    if not tickers:
        return pd.DataFrame()

    fmp = FMPClient()
    rows = []

    progress = st.progress(0, text="加载财务数据…")
    for i, (ticker, name) in enumerate(tickers):
        progress.progress((i + 1) / len(tickers), text=f"加载 {ticker}…")
        try:
            financials = fmp.get_financials(ticker, years=3)
            if not financials:
                rows.append({"Ticker": ticker, "公司": name})
                continue

            # 取最新年
            latest = max(financials, key=lambda r: getattr(r, "year", 0) or 0)
            prev_list = [r for r in financials if (getattr(r, "year", 0) or 0) < (getattr(latest, "year", 0) or 0)]
            prev = max(prev_list, key=lambda r: getattr(r, "year", 0) or 0) if prev_list else None

            rev = getattr(latest, "revenue", None)
            prev_rev = getattr(prev, "revenue", None) if prev else None
            rev_growth = (
                (float(rev) - float(prev_rev)) / abs(float(prev_rev)) * 100
                if rev and prev_rev and prev_rev != 0 else None
            )

            gm = getattr(latest, "gross_margin", None)
            ebitda = getattr(latest, "ebitda", None)
            capex = getattr(latest, "capex", None)
            nd_eq = getattr(latest, "net_debt_to_equity", None)
            year = getattr(latest, "year", None)

            rows.append({
                "Ticker": ticker,
                "公司": name,
                "最新FY": year,
                "Revenue": _fmt_b(rev),
                "YoY %": f"+{rev_growth:.1f}%" if rev_growth and rev_growth >= 0 else (f"{rev_growth:.1f}%" if rev_growth else "—"),
                "Gross Margin": _fmt_pct(gm),
                "EBITDA": _fmt_b(ebitda),
                "CAPEX": _fmt_b(capex),
                "Net Debt/Eq": _fmt_x(nd_eq),
                # 原始数值用于排序（隐藏列）
                "_rev_raw": float(rev) if rev else 0,
                "_gm_raw": (float(gm) * 100 if gm and float(gm) < 2 else float(gm)) if gm else 0,
                "_ebitda_raw": float(ebitda) if ebitda else 0,
            })
        except Exception:
            rows.append({"Ticker": ticker, "公司": name})

    progress.empty()
    return pd.DataFrame(rows)


# ── 主界面 ────────────────────────────────────────────────────────

sectors = _distinct_sectors()
if not sectors:
    st.warning("数据库中无活跃公司，请先写入 companies 表。")
    st.stop()

col1, col2 = st.columns([2, 1])
with col1:
    sector = st.selectbox("选择 Sector", sectors, key="matrix_sector")
with col2:
    sort_by = st.selectbox(
        "排序依据",
        ["Ticker", "Revenue↓", "Gross Margin↓", "EBITDA↓"],
        key="matrix_sort",
    )

if st.button("加载矩阵", type="primary"):
    df = _load_matrix(sector)
    st.session_state["_matrix_df"] = df
    st.session_state["_matrix_sector"] = sector

df: pd.DataFrame = st.session_state.get("_matrix_df", pd.DataFrame())
if not df.empty:
    st.success(f"**{st.session_state.get('_matrix_sector', sector)}** — {len(df)} 家公司")

    # 排序
    sort_map = {
        "Revenue↓": "_rev_raw",
        "Gross Margin↓": "_gm_raw",
        "EBITDA↓": "_ebitda_raw",
    }
    if sort_by in sort_map and sort_map[sort_by] in df.columns:
        df = df.sort_values(sort_map[sort_by], ascending=False)
    elif sort_by == "Ticker":
        df = df.sort_values("Ticker")

    # 显示列（隐藏原始数值列）
    display_cols = [c for c in df.columns if not c.startswith("_")]
    st.dataframe(
        df[display_cols],
        use_container_width=True,
        hide_index=True,
        height=min(60 + len(df) * 35, 700),
    )

    # 下载
    csv = df[display_cols].to_csv(index=False).encode("utf-8-sig")
    st.download_button(
        "⬇️ 下载 CSV",
        data=csv,
        file_name=f"sector_matrix_{sector}.csv",
        mime="text/csv",
    )

    # 迷你图：Revenue 横向柱状图
    if "_rev_raw" in df.columns and df["_rev_raw"].sum() > 0:
        st.divider()
        st.markdown("#### Revenue 规模对比")
        chart_df = df[df["_rev_raw"] > 0][["Ticker", "_rev_raw"]].copy()
        chart_df = chart_df.rename(columns={"_rev_raw": "Revenue (USD)"})
        chart_df = chart_df.sort_values("Revenue (USD)", ascending=True)
        chart_df = chart_df.set_index("Ticker")
        st.bar_chart(chart_df, height=max(200, len(chart_df) * 25))
