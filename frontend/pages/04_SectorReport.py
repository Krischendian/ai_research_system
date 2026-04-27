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
from research_automation.services.chart_service import (
    build_yoy_ranking_chart,
    build_financial_trend_chart,
    build_quarterly_sector_charts,
    build_quarterly_single_company_charts,
)
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
 
 
def _render_sector_summary_and_details(body: str, detail_label: str = "公司详情") -> None:
    """
    通用渲染：
    - COMPANY_DETAILS_START 之前的内容直接展示（Sector总结）
    - COMPANY_DETAILS_START 之后的内容按公司折叠
    """
    import re
 
    MARKER = "<!--- COMPANY_DETAILS_START --->"
 
    if MARKER in body:
        summary_part, details_part = body.split(MARKER, 1)
    else:
        summary_part = body
        details_part = ""
 
    # 渲染 Sector 总结
    if summary_part.strip():
        st.markdown(summary_part.strip())
 
    if not details_part.strip():
        return
 
    st.markdown("---")
    st.markdown(f"### 📂 各公司详情 — {detail_label}")
    # 公司详情折叠
    with st.expander("点击展开逐家查看 ↓", expanded=False):
        # 按 ### TICKER — 公司名 分割
        company_pattern = re.compile(
            r"^###\s+([\w/\-\.]+(?:\s+[\w/\-\.]+)?)\s+—\s+(.+)$",
            re.MULTILINE
        )
        matches = list(company_pattern.finditer(details_part))
 
        if not matches:
            st.markdown(details_part.strip())
            return
 
        for i, m in enumerate(matches):
            ticker = m.group(1).strip()
            company_name = m.group(2).strip()
            start = m.end()
            end = matches[i + 1].start() if i + 1 < len(matches) else len(details_part)
            company_body = details_part[start:end].strip()
 
            # 判断内容质量
            bad_keywords = ["逐字稿不可用", "画像生成失败", "暂无", "不可用"]
            has_issue = any(kw in company_body[:100] for kw in bad_keywords)
            label = f"{'⚠️' if has_issue else '✅'} {ticker} — {company_name}"
 
            with st.expander(label, expanded=False):
                st.markdown(company_body)
 
 
def _render_step6_section(body: str, sector_name: str) -> None:
    """Step6：个股快速扫描直接展示，图表折叠。"""
    # 快速扫描表在 --- 之前
    table_part = body.split("---", 1)[0].strip()

    if table_part:
        st.markdown(table_part)

    with st.expander("📈 财务趋势图表（点击展开）", expanded=False):
        _render_step6_charts(sector_name)
 
 
def _render_step6_charts(sector_name: str) -> None:
    """渲染Step6季度图表：板块汇总6图（2列3行）+ 各公司折叠。"""
    quarterly_data = st.session_state.get("_sector_report_quarterly", {})
 
    # 兼容两种结构：嵌套在 per_company_quarterly 里 或 直接是 ticker->rows
    if isinstance(quarterly_data, dict) and "per_company_quarterly" in quarterly_data:
        per_company_data = quarterly_data["per_company_quarterly"]
    elif isinstance(quarterly_data, dict) and quarterly_data:
        first_val = next(iter(quarterly_data.values()), None)
        per_company_data = quarterly_data if isinstance(first_val, list) else {}
    else:
        per_company_data = {}
 
    if not per_company_data:
        st.info("暂无季度财务数据（请重新生成报告以加载）。")
        _render_step6_charts_annual(sector_name)
        return
 
    # ── 板块汇总6图（严格2列3行，对应截图布局）────────────────
    st.markdown(f"#### 📊 {sector_name} — 板块季度财务图表")
    with st.spinner("生成板块汇总图表…"):
        sector_figs = build_quarterly_sector_charts(per_company_data, sector_name=sector_name)
 
    # 图顺序：[0]ROC [1]CAPEX柱 / [2]GM+ROC叠加 [3]GM折线 / [4]Revenue柱 [5]CAPEX vs Revenue
    for row in range(3):
        col1, col2 = st.columns(2)
        for col_idx, col in enumerate([col1, col2]):
            fig_idx = row * 2 + col_idx
            if fig_idx < len(sector_figs) and sector_figs[fig_idx]:
                with col:
                    st.plotly_chart(
                        sector_figs[fig_idx],
                        use_container_width=True,
                        key=f"sector_{sector_name}_fig_{fig_idx}",
                    )
 
    st.markdown("---")
 
    # ── 各公司单独6图（折叠显示）────────────────────────────
    st.markdown("#### 📈 各公司季度财务图表")
    for ticker, rows in sorted(per_company_data.items()):
        with st.expander(f"📌 {ticker} — 季度财务趋势", expanded=False):
            if not rows:
                st.info(f"{ticker} 暂无季度数据。")
                continue
            company_figs = build_quarterly_single_company_charts(ticker, rows)
            c1, c2 = st.columns(2)
            for i, fig in enumerate(company_figs):
                if fig:
                    with (c1 if i % 2 == 0 else c2):
                        st.plotly_chart(
                            fig,
                            use_container_width=True,
                            key=f"company_{ticker}_fig_{i}",
                        )
 
 
def _render_step6_charts_annual(sector_name: str) -> None:
    """降级方案：用年度数据渲染原有图表。"""
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
 
    def _g(obj, *keys):
        for k in keys:
            try:
                v = getattr(obj, k, None)
                if v is None and hasattr(obj, "__getitem__"):
                    v = obj[k]
                if v is not None:
                    return v
            except Exception:
                pass
        return None
 
    with st.spinner("加载年度财务趋势数据…"):
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
 
    fig_yoy = build_yoy_ranking_chart(financials_by_company, sector_name=sector_name)
    if fig_yoy:
        st.markdown("#### 📊 Revenue YoY 排名")
        st.plotly_chart(fig_yoy, use_container_width=True)
 
    fig = build_financial_trend_chart(financials_by_company, sector_name=sector_name)
    if fig:
        st.markdown("#### 📈 Sector 分位趋势（Top25% / 中位数 / Bottom25%）")
        st.plotly_chart(fig, use_container_width=True)
 
 
# ── 主渲染逻辑 ────────────────────────────────────────────────────
 
st.title("行业监控报告（六步结构）")
 
sectors = _distinct_sectors()
if not sectors:
    st.warning("数据库中无带 sector 的活跃公司。")
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
    st.caption("⚠️ 强制刷新会重新调用 LLM，约需 10–20 分钟并消耗约 $1–3 API 费用。")
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
        st.info(f"✅ 已有 {cy}Q{cq} 缓存报告，点击「生成报告」直接读取（秒级返回）。如需重新生成请勾选「强制刷新」。")
    else:
        st.warning(f"⚠️ 暂无 {cy}Q{cq} 缓存，首次生成需要约15-20分钟。")
 
if gen:
    with st.spinner("正在生成报告……"):
        try:
            report_md, quarterly_data = generate_six_step_sector_report(
                sector,
                relevance_threshold=int(thr),
                force_refresh=force_refresh,
            )
            st.session_state["_sector_report_md"] = report_md
            st.session_state["_sector_report_quarterly"] = quarterly_data
            st.session_state["_sector_report_name"] = sector
        except Exception as e:
            st.error(f"生成失败：{e}")
 
md = st.session_state.get("_sector_report_md")
if md:
    st.success(f"已生成：**{st.session_state.get('_sector_report_name', '')}**")
    st.download_button(
        "下载 Markdown",
        data=md.encode("utf-8"),
        file_name=f"sector_report_{st.session_state.get('_sector_report_name', 'report')}.md",
        mime="text/markdown",
    )
    # 目录
    st.markdown("""
---
### 📑 目录
1. [执行摘要](#执行摘要) — 财务快照、本季核心主题、重要事件、管理层信号
2. [业务占比](#step-2业务占比产品线地理收入) — 产品线 + 地理收入（FMP）
3. [展望与战略重心](#step-3展望与战略重心) — 10-K + Earning Call
4. [Earning Call 内容](#step-4-earning-call内容) — 逐字稿分析 + Quotations
5. [新业务 / 收购 / Insider](#step-5新业务收购insider异动) — Benzinga + FMP
6. [财务数据](#step-6财务数据年度) — FMP Annual Financials（3年）
7. [个股快速扫描](#个股快速扫描最新财年) — 最新财年横向对比
 
---
""")
    sections = _split_md_sections(md)
 
    ALWAYS_SHOW = {
        "__header__",
        "## 📋 执行摘要",
    }
    # 个股快速扫描移到最后，暂存内容
    snapshot_sections: list[tuple[str, str]] = []
 
    SKIP_SECTIONS = {"## Sector 总览"}
 
    # Step标题到中文标签的映射
    STEP_LABELS = {
        "Step 2": "业务占比",
        "Step 3": "展望与战略重心",
        "Step 4": "Earning Call",
        "Step 5": "新业务 / 收购 / Insider",
        "Step 6": "财务数据",
    }
 
    if 'sections' not in dir():
        sections = []
 
    for heading, body in sections:
        # 跳过废弃块
        if heading in SKIP_SECTIONS:
            continue
        if "Sector 整体总结" in heading or "Sector 季度总结" in heading:
            continue
 
        # 报告 header（标题、生成时间等）
        if heading == "__header__":
            st.markdown(body)
            continue
 
        # 执行摘要：直接展示，不折叠
        if heading in ALWAYS_SHOW:
            clean = heading.lstrip("#").strip()
            st.markdown(f"## {clean}")
            st.markdown(body)
            continue
 
        # 个股快速扫描：暂存，移到最后
        if "个股快速扫描" in heading:
            snapshot_sections.append((heading, body))
            continue
 
        # Step 2/3/4/5：Sector总结直接展示 + 公司详情折叠
        if any(s in heading for s in ["Step 2", "Step 3", "Step 4", "Step 5"]):
            clean = heading.lstrip("#").strip()
            st.markdown(f"## {clean}")
            detail_label = next(
                (v for k, v in STEP_LABELS.items() if k in heading),
                "公司详情"
            )
            _render_sector_summary_and_details(body, detail_label)
            continue
 
        # Step 6：个股扫描直接展示 + 图表折叠
        if "Step 6" in heading:
            clean = heading.lstrip("#").strip()
            st.markdown(f"## {clean}")
            _render_step6_section(
                body,
                st.session_state.get("_sector_report_name", "")
            )
            continue
 
        # 其他未知块：折叠显示
        label = heading.lstrip("#").strip()
        with st.expander(label, expanded=False):
            st.markdown(body)
 
    # 在 Step6 之后显示个股快速扫描
    for heading, body in snapshot_sections:
        clean = heading.lstrip("#").strip()
        st.markdown(f"## {clean}")
        st.markdown(body)