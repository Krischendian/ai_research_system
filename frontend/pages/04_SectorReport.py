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

    # 公司详情折叠
    with st.expander(f"📋 {detail_label}（点击展开）", expanded=False):
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
    parts = body.split("---", 1)
    table_part = parts[0].strip()
    rest_part = parts[1].strip() if len(parts) > 1 else ""

    if table_part:
        st.markdown(table_part)

    with st.expander("📈 财务趋势图表（点击展开）", expanded=False):
        _render_step6_charts(sector_name)

    if rest_part:
        with st.expander("📊 详细财务表格（点击展开）", expanded=False):
            st.markdown(rest_part)


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

    fig_yoy = build_yoy_ranking_chart(financials_by_company, sector_name=sector_name)
    if fig_yoy:
        st.markdown("#### 📊 Revenue YoY 排名")
        st.plotly_chart(fig_yoy, use_container_width=True)

    fig = build_financial_trend_chart(financials_by_company, sector_name=sector_name)
    if fig:
        st.markdown("#### 📈 Sector 分位趋势（Top25% / 中位数 / Bottom25%）")
        st.plotly_chart(fig, use_container_width=True)


# ── 主渲染逻辑 ────────────────────────────────────────────────────

ALWAYS_SHOW = {
    "__header__",
    "## 📋 执行摘要",
    "## 个股快速扫描（最新财年）",
    "## 个股快速扫描",
}

SKIP_SECTIONS = {"## Sector 总览"}

# Step标题到中文标签的映射
STEP_LABELS = {
    "Step 2": "业务占比",
    "Step 3": "展望与战略重心",
    "Step 4": "Earning Call",
    "Step 5": "新业务 / 收购 / Insider",
    "Step 6": "财务数据",
}

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

    # 执行摘要和个股快速扫描：直接展示，不折叠
    if heading in ALWAYS_SHOW:
        clean = heading.lstrip("#").strip()
        st.markdown(f"## {clean}")
        st.markdown(body)
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
            st.session_state.get("_sector_report_name", sector)
        )
        continue

    # 其他未知块：折叠显示
    label = heading.lstrip("#").strip()
    with st.expander(label, expanded=False):
        st.markdown(body)
