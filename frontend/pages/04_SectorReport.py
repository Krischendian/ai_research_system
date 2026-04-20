"""行业监控报告：六步结构版本。"""
from __future__ import annotations

import json
import re as _re
import sys
from pathlib import Path

_fe_root = Path(__file__).resolve().parent.parent.parent
_src = _fe_root / "src"
for p in (_fe_root, _src):
    if str(p) not in sys.path:
        sys.path.insert(0, str(p))

import streamlit as st
from datetime import datetime, timezone
from dotenv import load_dotenv

from research_automation.core.database import get_connection, init_db
from research_automation.services.report_cache import (
    delete_report_cache,
    get_cached_report,
    list_cached_reports,
)
from research_automation.services.chart_service import build_financial_trend_chart, build_yoy_ranking_chart
from research_automation.extractors.fmp_client import FMPClient
from research_automation.services.sector_report_service import generate_six_step_sector_report

load_dotenv(_fe_root / ".env", override=False)


def _distinct_sectors() -> list[str]:
    conn = get_connection()
    try:
        init_db(conn)
        cur = conn.execute(
            """
            SELECT DISTINCT sector FROM companies
            WHERE is_active = 1 AND TRIM(sector) != ''
            ORDER BY sector
            """
        )
        return [str(r[0]).strip() for r in cur.fetchall() if r[0]]
    finally:
        conn.close()


def _split_md_sections(md: str) -> list[tuple[str, str]]:
    import re
    parts: list[tuple[str, str]] = []
    pattern = re.compile(r"^(##\s+.+)$", re.MULTILINE)
    matches = list(pattern.finditer(md))
    if not matches:
        return [("__header__", md)]
    preamble = md[: matches[0].start()].strip()
    if preamble:
        parts.append(("__header__", preamble))
    for i, m in enumerate(matches):
        heading = m.group(1).strip()
        start = m.end()
        end = matches[i + 1].start() if i + 1 < len(matches) else len(md)
        body = md[start:end].strip()
        parts.append((heading, body))
    return parts


def _parse_step1_data(body: str) -> dict[str, list[dict]]:
    """从Step1 markdown body解析出各公司的业务线数据，用于图表。"""
    result = {}
    current_company = None
    current_ticker = None
    segments = []

    for line in body.splitlines():
        # 匹配公司行：**Accenture (ACN)**　总收入 $64.9B
        m = _re.match(r"\*\*(.+?)\s*\((\w[\w/\s]*)\)\*\*", line)
        if m:
            if current_ticker and segments:
                result[current_ticker] = segments
            current_company = m.group(1).strip()
            current_ticker = m.group(2).strip()
            segments = []
            continue
        # 匹配业务线行：- Products: 30.1%　███　($19.55B)
        m2 = _re.match(r"-\s+(.+?):\s+([\d.]+)%", line)
        if m2 and current_ticker:
            segments.append({
                "segment": m2.group(1).strip(),
                "percentage": float(m2.group(2)),
            })

    if current_ticker and segments:
        result[current_ticker] = segments

    return result


def _render_step1_charts(body: str) -> None:
    """用st.bar_chart渲染Step1各公司业务占比图表。"""
    data = _parse_step1_data(body)
    if not data:
        st.markdown(body)
        return

    st.markdown(body)
    st.divider()
    st.markdown("#### 📊 业务线占比图表")

    cols = st.columns(min(len(data), 3))
    for i, (ticker, segs) in enumerate(data.items()):
        col = cols[i % len(cols)]
        with col:
            st.markdown(f"**{ticker}**")
            chart_data = {s["segment"]: s["percentage"] for s in segs}
            import pandas as pd
            df = pd.DataFrame.from_dict(
                chart_data, orient="index", columns=["占比%"]
            )
            st.bar_chart(df, height=200)


def _render_step2_tables(body: str) -> None:
    """Step2直接渲染markdown表格，Streamlit原生支持。"""
    st.markdown(body)


def _render_step6_charts(sector_name: str) -> None:
    """从FMP拉取各公司年度财务，渲染Plotly趋势图。"""
    conn = get_connection()
    try:
        init_db(conn)
        cur = conn.execute(
            "SELECT ticker FROM companies WHERE is_active=1 AND TRIM(sector)=? ORDER BY ticker",
            (sector_name,),
        )
        tickers = [r[0] for r in cur.fetchall()]
    finally:
        conn.close()

    if not tickers:
        return

    fmp = FMPClient()
    financials_by_company: dict[str, list[dict]] = {}

    def _g(obj: object, *keys: str) -> object | None:
        for k in keys:
            try:
                v = getattr(obj, k, None)
                if v is None and hasattr(obj, "__getitem__"):
                    v = obj[k]  # type: ignore[index]
                if v is not None:
                    return v
            except Exception:
                pass
        return None

    with st.spinner("加载财务趋势数据…"):
        for ticker in tickers:
            try:
                rows = fmp.get_financials(ticker, years=3)
                if not rows:
                    continue
                parsed = []
                for r in rows:
                    gm = _g(r, "gross_margin_pct", "gross_margin")
                    if gm is not None and isinstance(gm, (int, float)) and gm < 2:
                        gm = float(gm) * 100

                    parsed.append({
                        "year": _g(r, "year", "fiscal_year"),
                        "revenue": _g(r, "revenue"),
                        "gross_margin_pct": gm,
                        "capex": _g(r, "capex"),
                        "ebitda": _g(r, "ebitda"),
                    })

                parsed.sort(key=lambda x: x.get("year") or 0)
                financials_by_company[ticker] = parsed
            except Exception:
                continue

    if not financials_by_company:
        st.info("暂无财务趋势数据。")
        return

    # 图1：Revenue YoY 排名图
    fig_yoy = build_yoy_ranking_chart(financials_by_company, sector_name=sector_name)
    if fig_yoy:
        st.divider()
        st.markdown("#### 📊 Revenue YoY 排名")
        st.plotly_chart(fig_yoy, use_container_width=True)

    # 图2：Sector 分位趋势图
    fig = build_financial_trend_chart(financials_by_company, sector_name=sector_name)
    if fig:
        st.markdown("#### 📈 Sector 分位趋势（Top25% / 中位数 / Bottom25%）")
        st.plotly_chart(fig, use_container_width=True)


st.title("行业监控报告（六步结构）")

sectors = _distinct_sectors()
if not sectors:
    st.warning(
        "数据库中无带 sector 的活跃公司（`companies.is_active=1`）。"
        "请先向 `companies` 表写入标的。"
    )
    st.stop()

c1, c2, c3 = st.columns([2, 1, 1])
with c1:
    sector = st.selectbox("选择 sector", sectors, key="sector_report_pick")
with c2:
    thr = st.number_input(
        "`relevance_score` 下限（0–3）",
        min_value=0, max_value=3, value=1,
    )
with c3:
    force_refresh = st.checkbox("强制刷新", value=False)
    gen = st.button("生成报告", type="primary")


def _current_cache_quarter() -> tuple[int, int]:
    now = datetime.now(timezone.utc)
    q = (now.month - 1) // 3 + 1
    y = now.year
    if q == 1:
        return y - 1, 4
    return y, q - 1


if sector:
    cy, cq = _current_cache_quarter()
    cached = get_cached_report(sector, cy, cq)
    if cached:
        st.info(
            f"✅ 已有 {cy}Q{cq} 缓存报告，点击「生成报告」直接读取缓存（秒级返回）。"
            "如需重新生成请勾选「强制刷新」。"
        )
    else:
        st.warning(f"⚠️ 暂无 {cy}Q{cq} 缓存，首次生成需要约5-10分钟。")

if gen:
    st.session_state.pop("_sector_report_error", None)
    with st.spinner("正在生成六步结构报告（包含 Earning Call 分析，可能需要2-3分钟）……"):
        try:
            st.session_state["_sector_report_md"] = generate_six_step_sector_report(
                sector,
                relevance_threshold=int(thr),
                force_refresh=force_refresh,
            )
            st.session_state["_sector_report_name"] = sector
        except Exception as e:
            st.session_state["_sector_report_error"] = str(e)

err = st.session_state.pop("_sector_report_error", None)
if err:
    st.error(err)

md = st.session_state.get("_sector_report_md")
if md:
    st.success(f"已生成：**{st.session_state.get('_sector_report_name', sector)}**")

    st.download_button(
        "下载 Markdown",
        data=md.encode("utf-8"),
        file_name=f"sector_report_{sector}.md",
        mime="text/markdown",
    )

    sections = _split_md_sections(md)

    # 始终展开的顶部区域
    ALWAYS_EXPANDED = {"__header__", "## 📋 执行摘要", "## 个股快速扫描（最新财年）"}
    # 默认折叠的详细内容
    DEFAULT_COLLAPSED = {
        "## Step 2｜业务占比（产品线 + 地理收入）",
        "## Step 3｜展望与战略重心",
        "## Step 4｜Earning Call 内容",
        "## Step 5｜新业务 / 收购 / Insider 异动",
        "## Step 6｜财务数据（年度）",
    }

    for heading, body in sections:
        if heading == "__header__":
            st.markdown(body)
            continue

        if heading in ALWAYS_EXPANDED:
            # 执行摘要和个股扫描始终展开
            st.markdown(f"### {heading.lstrip('#').strip()}")
            st.markdown(body)
            continue

        # 其余内容折叠
        label = heading.lstrip("#").strip()
        expanded = heading not in DEFAULT_COLLAPSED

        with st.expander(label, expanded=expanded):
            if "Step 2" in heading or "Step 1" in heading:
                _render_step2_tables(body)
            elif "Step 6" in heading:
                st.markdown(body)
                _render_step6_charts(
                    st.session_state.get("_sector_report_name", sector)
                )
            else:
                st.markdown(body)