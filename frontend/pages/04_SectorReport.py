"""行业监控报告：六步结构版本。"""
from __future__ import annotations

import sys
from pathlib import Path

_fe_root = Path(__file__).resolve().parent.parent.parent
_src = _fe_root / "src"
for p in (_fe_root, _src):
    if str(p) not in sys.path:
        sys.path.insert(0, str(p))

import streamlit as st
from dotenv import load_dotenv

from research_automation.core.database import get_connection, init_db
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
    gen = st.button("生成报告", type="primary")

if gen:
    st.session_state.pop("_sector_report_error", None)
    with st.spinner("正在生成六步结构报告（包含 Earning Call 分析，可能需要2-3分钟）……"):
        try:
            st.session_state["_sector_report_md"] = generate_six_step_sector_report(
                sector,
                relevance_threshold=int(thr),
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
            st.markdown(body)