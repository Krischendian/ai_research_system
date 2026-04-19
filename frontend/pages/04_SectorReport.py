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

    col_dl, col_mode = st.columns([3, 1])
    with col_dl:
        st.download_button(
            "下载 Markdown",
            data=md.encode("utf-8"),
            file_name=f"sector_report_{sector}.md",
            mime="text/markdown",
        )
    with col_mode:
        brief_mode = st.toggle("简报模式（1-2屏）", value=False)

    sections = _split_md_sections(md)

    BRIEF_WHITELIST = {
        "__header__",
        "## Step 1｜Sector 业务全景",
        "## Step 4｜Earning Call 内容",
        "## Step 5｜新业务 / 收购 / Insider 异动",
    }

    DEFAULT_EXPANDED = {
        "## Step 1｜Sector 业务全景",
        "## Step 4｜Earning Call 内容",
        "## Step 5｜新业务 / 收购 / Insider 异动",
    }

    for heading, body in sections:
        if brief_mode and heading not in BRIEF_WHITELIST:
            continue
        if heading == "__header__":
            st.markdown(body)
            continue
        if brief_mode:
            st.markdown(f"{heading}\n\n{body}")
            continue
        expanded = heading in DEFAULT_EXPANDED
        with st.expander(heading.lstrip("#").strip(), expanded=expanded):
            if "Step 1" in heading:
                _render_step1_charts(body)
            elif "Step 2" in heading:
                _render_step2_tables(body)
            else:
                st.markdown(body)