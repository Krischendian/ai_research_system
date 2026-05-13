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

# ── 页面配置 ──────────────────────────────────────────────────────
st.set_page_config(
    page_title="行业监控报告",
    page_icon="📊",
    layout="wide",
    initial_sidebar_state="collapsed",
)

# ── 全局样式 ──────────────────────────────────────────────────────
st.markdown("""
<style>
/* 主色调变量 */
:root {
    --primary: #1a56db;
    --primary-light: #e8f0fe;
    --success: #0e9f6e;
    --warning: #c27803;
    --danger: #e02424;
    --gray-50: #f9fafb;
    --gray-100: #f3f4f6;
    --gray-200: #e5e7eb;
    --gray-600: #4b5563;
    --gray-800: #1f2937;
    --border-radius: 8px;
}

/* 隐藏默认 Streamlit 元素 */
#MainMenu, footer, header { visibility: hidden; }
.block-container { padding-top: 1.5rem; padding-bottom: 2rem; max-width: 1400px; }

/* 页面标题 */
.report-title {
    font-size: 1.6rem;
    font-weight: 700;
    color: var(--gray-800);
    letter-spacing: -0.02em;
    margin-bottom: 0.25rem;
    border-bottom: 3px solid var(--primary);
    padding-bottom: 0.5rem;
    display: inline-block;
}

/* Section 标题卡片 */
.section-header {
    background: linear-gradient(135deg, var(--primary) 0%, #1e40af 100%);
    color: white;
    padding: 0.6rem 1.2rem;
    border-radius: var(--border-radius);
    font-size: 1rem;
    font-weight: 600;
    margin: 1.5rem 0 0.75rem 0;
    display: flex;
    align-items: center;
    gap: 0.5rem;
}

/* 公司卡片 */
.company-card {
    background: white;
    border: 1px solid var(--gray-200);
    border-radius: var(--border-radius);
    padding: 1rem 1.2rem;
    margin-bottom: 0.75rem;
    border-left: 4px solid var(--primary);
    transition: box-shadow 0.15s;
}
.company-card:hover { box-shadow: 0 2px 8px rgba(0,0,0,0.08); }
.company-card-header {
    font-size: 0.95rem;
    font-weight: 600;
    color: var(--gray-800);
    margin-bottom: 0.4rem;
}
.company-card-meta {
    font-size: 0.8rem;
    color: var(--gray-600);
}

/* 指标徽章 */
.metric-badge {
    display: inline-flex;
    align-items: center;
    gap: 0.3rem;
    padding: 0.2rem 0.6rem;
    border-radius: 20px;
    font-size: 0.78rem;
    font-weight: 600;
}
.metric-badge.up { background: #d1fae5; color: #065f46; }
.metric-badge.down { background: #fee2e2; color: #991b1b; }
.metric-badge.neutral { background: var(--gray-100); color: var(--gray-600); }

/* 状态指示点 */
.status-dot {
    width: 8px; height: 8px;
    border-radius: 50%;
    display: inline-block;
    margin-right: 4px;
}
.status-dot.ok { background: var(--success); }
.status-dot.warn { background: var(--warning); }
.status-dot.err { background: var(--danger); }

/* 内容区域 */
.content-block {
    background: var(--gray-50);
    border: 1px solid var(--gray-200);
    border-radius: var(--border-radius);
    padding: 1rem 1.2rem;
    margin-bottom: 0.5rem;
    font-size: 0.9rem;
    line-height: 1.7;
}

/* 执行摘要 Tab 内容 */
.exec-tab-content {
    padding: 0.5rem 0;
}

/* 覆盖率指示条 */
.coverage-bar-wrap {
    background: var(--gray-200);
    border-radius: 20px;
    height: 6px;
    width: 100%;
    margin-top: 4px;
}
.coverage-bar-fill {
    background: var(--primary);
    border-radius: 20px;
    height: 6px;
}

/* expander 美化 */
.streamlit-expanderHeader {
    background: var(--gray-50) !important;
    border-radius: var(--border-radius) !important;
    font-size: 0.9rem !important;
}

/* 分隔线 */
.section-divider {
    border: none;
    border-top: 1px solid var(--gray-200);
    margin: 1rem 0;
}

/* 下载按钮 */
.stDownloadButton > button {
    background: white !important;
    border: 1.5px solid var(--primary) !important;
    color: var(--primary) !important;
    font-weight: 600 !important;
    border-radius: 6px !important;
}

/* 生成按钮 */
.stButton > button[kind="primary"] {
    background: var(--primary) !important;
    border: none !important;
    border-radius: 6px !important;
    font-weight: 600 !important;
    padding: 0.5rem 1.5rem !important;
}
</style>
""", unsafe_allow_html=True)


def _safe_md(text: str) -> str:
    if not isinstance(text, str):
        return text
    return text.replace("$", r"\$")


def _distinct_sectors() -> list[str]:
    conn = get_connection()
    try:
        init_db(conn)
        cur = conn.execute(
            "SELECT DISTINCT sector FROM companies WHERE is_active = 1 AND TRIM(sector) != \'\' ORDER BY sector"
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


def _section_header(icon: str, title: str) -> None:
    st.markdown(
        f'''<div class="section-header">{icon} {title}</div>''',
        unsafe_allow_html=True,
    )


def _render_exec_summary_tabs(body: str) -> None:
    """执行摘要分 Tab 展示。"""
    import re

    # 找各子板块
    tab_patterns = {
        "🔑 核心主题": r"###\s*🔑.*?(?=###|\Z)",
        "⚡ 重要事件": r"###\s*⚡.*?(?=###|\Z)",
        "💬 管理层信号": r"###\s*💬.*?(?=###|\Z)",
        "⚠️ 主要风险": r"###\s*⚠️.*?(?=###|\Z)",
        "🏆 相对排名": r"###\s*🏆.*?(?=###|\Z)",
    }

    tab_contents: dict[str, str] = {}
    for label, pattern in tab_patterns.items():
        m = re.search(pattern, body, re.DOTALL)
        if m:
            # 去掉 ### 标题行本身
            content = re.sub(r"^###.*?\n", "", m.group(0), count=1).strip()
            tab_contents[label] = content

    # 顶部免责声明
    disclaimer_m = re.search(r"> ⚠️.*?仅供参考.*?\n", body)
    if disclaimer_m:
        st.markdown(_safe_md(disclaimer_m.group(0)))

    if not tab_contents:
        st.markdown(_safe_md(body))
        return

    tabs = st.tabs(list(tab_contents.keys()))
    for tab, (label, content) in zip(tabs, tab_contents.items()):
        with tab:
            st.markdown(_safe_md(content))


def _render_sector_summary_and_details(body: str, detail_label: str = "公司详情") -> None:
    import re
    MARKER = "<!--- COMPANY_DETAILS_START --->"

    if MARKER in body:
        summary_part, details_part = body.split(MARKER, 1)
    else:
        summary_part = body
        details_part = ""

    if summary_part.strip():
        st.markdown(_safe_md(summary_part.strip()))

    if not details_part.strip():
        return

    st.markdown('<hr class="section-divider">', unsafe_allow_html=True)

    # 解析各公司
    company_pattern = re.compile(
        r"^###\s+([\w/\-\.]+(?:\s+[\w/\-\.]+)?)\s+—\s+(.+)$",
        re.MULTILINE,
    )
    matches = list(company_pattern.finditer(details_part))

    if not matches:
        with st.expander(f"📂 {detail_label} 详情", expanded=False):
            st.markdown(_safe_md(details_part.strip()))
        return

    # 统计有/无数据公司数
    bad_keywords = ["逐字稿暂不可用", "逐字稿不可用", "画像生成失败", "暂无", "不可用", "无分析结果"]
    ok_count = 0
    warn_count = 0
    company_items = []
    for i, m in enumerate(matches):
        ticker = m.group(1).strip()
        company_name = m.group(2).strip()
        start = m.end()
        end = matches[i + 1].start() if i + 1 < len(matches) else len(details_part)
        company_body = details_part[start:end].strip()
        has_issue = any(kw in company_body[:150] for kw in bad_keywords)
        if has_issue:
            warn_count += 1
        else:
            ok_count += 1
        company_items.append((ticker, company_name, company_body, has_issue))

    total = ok_count + warn_count
    coverage_pct = int(ok_count / total * 100) if total > 0 else 0

    col_info, col_bar = st.columns([3, 1])
    with col_info:
        st.markdown(
            f'''<div style="font-size:0.85rem;color:#4b5563;margin-bottom:0.5rem;">
            <span class="status-dot ok"></span>{ok_count} 家有完整数据　
            <span class="status-dot warn" style="margin-left:8px"></span>{warn_count} 家数据缺失
            　（{detail_label}，共 {total} 家）
            </div>''',
            unsafe_allow_html=True,
        )
    with col_bar:
        st.markdown(
            f'''<div style="padding-top:6px">
            <div class="coverage-bar-wrap">
              <div class="coverage-bar-fill" style="width:{coverage_pct}%"></div>
            </div>
            <div style="font-size:0.75rem;color:#6b7280;text-align:right">{coverage_pct}% 覆盖</div>
            </div>''',
            unsafe_allow_html=True,
        )

    for ticker, company_name, company_body, has_issue in company_items:
        icon = "⚠️" if has_issue else "✅"
        label = f"{icon} {ticker} — {company_name}"
        with st.expander(label, expanded=False):
            st.markdown(_safe_md(company_body))


def _render_company_cards(body: str) -> None:
    """各公司业务简介：卡片式布局。"""
    import re

    company_pattern = re.compile(
        r"^###\s+([\w/\-\.]+(?:\s+[\w/\-\.]+)?)\s+—\s+(.+)$",
        re.MULTILINE,
    )
    matches = list(company_pattern.finditer(body))

    if not matches:
        st.markdown(_safe_md(body))
        return

    # 解析各公司
    companies = []
    for i, m in enumerate(matches):
        ticker = m.group(1).strip()
        company_name = m.group(2).strip()
        start = m.end()
        end = matches[i + 1].start() if i + 1 < len(matches) else len(body)
        company_body = body[start:end].strip()
        companies.append((ticker, company_name, company_body))

    # 用 expander 展示，每行3列
    cols_per_row = 3
    for row_start in range(0, len(companies), cols_per_row):
        row = companies[row_start : row_start + cols_per_row]
        cols = st.columns(cols_per_row)
        for col, (ticker, company_name, company_body) in zip(cols, row):
            with col:
                with st.expander(f"🏢 **{ticker}** — {company_name}", expanded=False):
                    st.markdown(_safe_md(company_body))


def _render_snapshot_table(body: str) -> None:
    """个股快速扫描：渲染为带颜色的 dataframe。"""
    import re
    import pandas as pd

    # 尝试解析 markdown 表格
    lines = [l for l in body.split("\n") if l.strip()]
    table_lines = [l for l in lines if l.strip().startswith("|")]

    if len(table_lines) >= 3:
        try:
            # 解析表头
            header = [c.strip() for c in table_lines[0].split("|") if c.strip()]
            # 跳过分隔行
            data_rows = []
            for l in table_lines[2:]:
                row = [c.strip() for c in l.split("|") if c.strip()]
                if row:
                    data_rows.append(row)

            if data_rows:
                # 补齐列数
                max_cols = len(header)
                data_rows = [r + [""] * (max_cols - len(r)) for r in data_rows]
                df = pd.DataFrame(data_rows, columns=header[:max_cols])

                # YoY 列着色
                def _color_yoy(val):
                    if not isinstance(val, str):
                        return ""
                    if "+" in val:
                        return "background-color: #d1fae5; color: #065f46"
                    if val.startswith("-"):
                        return "background-color: #fee2e2; color: #991b1b"
                    return ""

                if "YoY" in df.columns:
                    styled = df.style.applymap(_color_yoy, subset=["YoY"])
                    st.dataframe(styled, use_container_width=True, hide_index=True)
                else:
                    st.dataframe(df, use_container_width=True, hide_index=True)

                # 底注
                note_lines = [l for l in lines if not l.strip().startswith("|")]
                if note_lines:
                    st.caption(_safe_md(" ".join(note_lines)))
                return
        except Exception:
            pass

    # 降级：直接渲染 markdown
    st.markdown(_safe_md(body))


def _render_step6_section(body: str, sector_name: str) -> None:
    table_part = body.split("---", 1)[0].strip()
    if table_part:
        _render_snapshot_table(table_part)
    with st.expander("📈 财务趋势图表（点击展开）", expanded=False):
        _render_step6_charts(sector_name)


def _render_step6_charts(sector_name: str) -> None:
    quarterly_data = st.session_state.get("_sector_report_quarterly", {})

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

    st.markdown(f"#### 📊 {sector_name} — 板块季度财务图表")
    with st.spinner("生成板块汇总图表…"):
        sector_figs = build_quarterly_sector_charts(per_company_data, sector_name=sector_name)

    for row in range(3):
        col1, col2 = st.columns(2)
        for col_idx, col in enumerate([col1, col2]):
            fig_idx = row * 2 + col_idx
            if fig_idx < len(sector_figs) and sector_figs[fig_idx]:
                with col:
                    st.plotly_chart(
                        sector_figs[fig_idx],
                        width="stretch",
                        key=f"sector_{sector_name}_fig_{fig_idx}",
                    )

    st.markdown("---")
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
                            width="stretch",
                            key=f"company_{ticker}_fig_{i}",
                        )


def _render_step6_charts_annual(sector_name: str) -> None:
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
        st.plotly_chart(fig_yoy, width="stretch")

    fig = build_financial_trend_chart(financials_by_company, sector_name=sector_name)
    if fig:
        st.markdown("#### 📈 Sector 分位趋势（Top25% / 中位数 / Bottom25%）")
        st.plotly_chart(fig, width="stretch")


# ── 主渲染逻辑 ────────────────────────────────────────────────────

st.markdown('<div class="report-title">📊 行业监控报告（六步结构）</div>', unsafe_allow_html=True)
st.markdown("")

sectors = _distinct_sectors()
if not sectors:
    st.warning("数据库中无带 sector 的活跃公司。")
    st.stop()

# 控制栏
col_sel, col_thr, col_opt, col_btn = st.columns([3, 1, 1, 1])
with col_sel:
    sector = st.selectbox("选择板块", sectors, key="sector_report_pick", label_visibility="collapsed")
with col_thr:
    thr = st.number_input("相关性下限", min_value=0, max_value=3, value=1, help="新闻 relevance_score 下限（0–3）")
with col_opt:
    force_refresh = st.checkbox("强制刷新", value=False, help="⚠️ 重新调用 LLM，约需 10–20 分钟并消耗约 1–3 美元 API 费用")
with col_btn:
    st.markdown("<div style='padding-top:4px'>", unsafe_allow_html=True)
    gen = st.button("生成报告 →", type="primary", use_container_width=True)
    st.markdown("</div>", unsafe_allow_html=True)


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
        st.success(f"✅ 已有 {cy}Q{cq} 缓存报告，点击「生成报告」直接读取（秒级返回）。如需重新生成请勾选「强制刷新」。")
    else:
        st.warning(f"⚠️ 暂无 {cy}Q{cq} 缓存，首次生成需要约 15–20 分钟。")

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
if not md:
    st.stop()

sector_name = st.session_state.get("_sector_report_name", "")

# 顶部操作栏
col_dl, col_info = st.columns([1, 4])
with col_dl:
    st.download_button(
        "⬇️ 下载 Markdown",
        data=md.encode("utf-8"),
        file_name=f"sector_report_{sector_name}.md",
        mime="text/markdown",
    )

st.markdown("---")

sections = _split_md_sections(md)

SKIP_SECTIONS = {"## Sector 总览"}
snapshot_sections: list[tuple[str, str]] = []

STEP_LABELS = {
    "Step 2": "业务占比",
    "Step 3": "展望与战略重心",
    "Step 4": "Earning Call",
    "Step 5": "新业务 / 收购 / Insider",
    "Step 6": "财务数据",
}

for heading, body in sections:
    if heading in SKIP_SECTIONS:
        continue
    if "Sector 整体总结" in heading or "Sector 季度总结" in heading:
        continue

    # ── 报告 header ──
    if heading == "__header__":
        # 提取生成时间和新闻窗口
        import re
        gen_time = re.search(r"\*\*生成时间\*\*：(.+)", body)
        news_win = re.search(r"\*\*新闻窗口\*\*：(.+)", body)
        meta_parts = []
        if gen_time:
            meta_parts.append(f"🕐 {gen_time.group(1).strip()}")
        if news_win:
            meta_parts.append(f"📰 {news_win.group(1).strip()}")
        if meta_parts:
            st.markdown(
                f'''<div style="font-size:0.82rem;color:#6b7280;margin-bottom:0.5rem;">{"　｜　".join(meta_parts)}</div>''',
                unsafe_allow_html=True,
            )
        continue

    # ── 板块概览（Step 0）──
    if "板块概览" in heading:
        _section_header("📊", "板块概览")
        st.markdown(_safe_md(body))
        continue

    # ── 各公司业务简介 ──
    if "各公司业务简介" in heading:
        _section_header("🏢", "各公司业务简介")
        _render_company_cards(body)
        continue

    # ── 执行摘要：分 Tab ──
    if "执行摘要" in heading:
        _section_header("📋", "执行摘要")
        _render_exec_summary_tabs(body)
        continue

    # ── 个股快速扫描：暂存 ──
    if "个股快速扫描" in heading:
        snapshot_sections.append((heading, body))
        continue

    # ── Step 2/3/4/5：Sector总结 + 公司折叠 ──
    if any(s in heading for s in ["Step 2", "Step 3", "Step 4", "Step 5"]):
        clean = heading.lstrip("#").strip()
        _section_header("📂", clean)
        detail_label = next(
            (v for k, v in STEP_LABELS.items() if k in heading), "公司详情"
        )
        _render_sector_summary_and_details(body, detail_label)
        continue

    # ── Step 6 ──
    if "Step 6" in heading:
        clean = heading.lstrip("#").strip()
        _section_header("📈", clean)
        _render_step6_section(body, sector_name)
        continue

    # ── 其他 ──
    label = heading.lstrip("#").strip()
    with st.expander(label, expanded=False):
        st.markdown(_safe_md(body))

# 个股快速扫描放最后
for heading, body in snapshot_sections:
    clean = heading.lstrip("#").strip()
    _section_header("⚡", clean)
    _render_snapshot_table(body)