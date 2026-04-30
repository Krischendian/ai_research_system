"""自动化晨报：RSS + 后端 LLM 摘要。"""
from __future__ import annotations

import sys
from pathlib import Path

_fe_root = Path(__file__).resolve().parent.parent.parent
if str(_fe_root) not in sys.path:
    sys.path.insert(0, str(_fe_root))

import requests
import streamlit as st

from frontend.morning_brief_helpers import (
    build_morning_brief_news_markdown,
    format_ny_badge,
)
from frontend.streamlit_helpers import format_api_error

BACKEND_BASE = "http://127.0.0.1:8000"
_PROJECT_ROOT = _fe_root

REGION_COLOR = {
    "North America": "#1a73e8",
    "Europe": "#34a853",
    "Middle East": "#fbbc04",
    "Asia": "#ea4335",
    "Global": "#9e9e9e",
}

REGION_PILL_CLASS = {
    "North America": "rp-na",
    "Europe": "rp-eu",
    "Middle East": "rp-me",
    "Asia": "rp-as",
    "Global": "rp-gl",
}

REGION_ORDER = ["North America", "Europe", "Middle East", "Asia", "Global"]

if "brief_cache_key" not in st.session_state:
    st.session_state.brief_cache_key = 0

st.title("自动化晨报")

AVAILABLE_SECTORS = ["AI_Job_Replacement", "Natural_Gas", "Technology"]

sector = st.selectbox(
    "监控板块",
    options=AVAILABLE_SECTORS,
    index=0,
    key="selected_sector",
)

c1, c2 = st.columns([1, 5])
with c1:
    if st.button("刷新"):
        st.session_state.brief_cache_key += 1


@st.cache_data(ttl=120)
def _load_overnight(_backend: str, _cache_key: int, _sector: str) -> dict:
    r = requests.get(
        f"{_backend}/api/v1/news/overnight",
        params={"sector": _sector},
        timeout=120,
    )
    r.raise_for_status()
    return r.json()


@st.cache_data(ttl=300)
def _load_yesterday_summary(_backend: str, _cache_key: int, _sector: str) -> dict:
    r = requests.get(
        f"{_backend}/api/v1/news/yesterday-summary",
        params={"sector": _sector},
        timeout=180,
    )
    r.raise_for_status()
    return r.json()


@st.cache_data(ttl=120)
def _load_morning_brief(_backend: str, _cache_key: int) -> dict:
    r = requests.get(
        f"{_backend}/api/v1/news/morning-brief",
        timeout=180,
    )
    r.raise_for_status()
    return r.json()


def _escape_html(s: str) -> str:
    return (
        (s or "")
        .replace("&", "&amp;")
        .replace("<", "&lt;")
        .replace(">", "&gt;")
    )


def _inject_styles() -> None:
    st.markdown(
        """
<style>
.news-two-col {
    display: grid;
    grid-template-columns: 1fr 1fr;
    gap: 12px;
    margin-top: 4px;
}
.region-col { display: flex; flex-direction: column; }
.region-pill {
    display: inline-block;
    font-size: 10px;
    font-weight: 500;
    padding: 2px 8px;
    border-radius: 999px;
    margin: 6px 0 4px;
}
.rp-na { background:#E6F1FB; color:#0C447C; }
.rp-eu { background:#EAF3DE; color:#27500A; }
.rp-me { background:#FAEEDA; color:#633806; }
.rp-as { background:#FAECE7; color:#712B13; }
.rp-gl { background:#F1EFE8; color:#444441; }
.news-compact {
    border-top: 0.5px solid rgba(0,0,0,0.08);
    padding: 6px 0;
}
.news-compact:first-child { border-top: none; }
.news-title-row {
    display: flex;
    align-items: baseline;
    gap: 5px;
    margin-bottom: 2px;
}
.news-star { font-size: 10px; color: #BA7517; font-weight: 500; }
.news-title-text {
    font-size: 12px;
    font-weight: 500;
    color: var(--color-text-primary, #111);
    line-height: 1.35;
}
.news-meta-row {
    font-size: 10px;
    color: var(--color-text-tertiary, #888);
    margin-bottom: 2px;
}
.news-meta-row a { color: #1a73e8; text-decoration: none; }
.news-summary-text {
    font-size: 11px;
    color: var(--color-text-secondary, #555);
    line-height: 1.5;
}
.co-table { width: 100%; border-collapse: collapse; }
.co-table tr { border-bottom: 0.5px solid rgba(0,0,0,0.08); }
.co-table tr:last-child { border-bottom: none; }
.co-table td { padding: 7px 4px; vertical-align: top; }
.ticker-badge {
    display: inline-block;
    font-size: 10px;
    font-weight: 500;
    padding: 2px 6px;
    border-radius: 4px;
    background: #1a1a2e;
    color: #fff;
    white-space: nowrap;
}
.event-tag {
    font-size: 10px;
    padding: 1px 5px;
    border-radius: 3px;
    background: rgba(0,0,0,0.06);
    color: var(--color-text-secondary, #555);
    white-space: nowrap;
}
.co-title-row {
    display: flex;
    align-items: baseline;
    gap: 5px;
    margin-bottom: 2px;
    flex-wrap: wrap;
}
.co-star { font-size: 10px; color: #BA7517; white-space: nowrap; }
.co-title-text {
    font-size: 12px;
    font-weight: 500;
    color: var(--color-text-primary, #111);
    line-height: 1.3;
}
.co-summary-text {
    font-size: 11px;
    color: var(--color-text-secondary, #555);
    line-height: 1.5;
    margin-top: 1px;
}
.co-meta-text {
    font-size: 10px;
    color: var(--color-text-tertiary, #888);
    margin-top: 2px;
}
.co-meta-text a { color: #1a73e8; text-decoration: none; }
.no-news-row {
    font-size: 11px;
    color: var(--color-text-tertiary, #888);
    padding: 4px 0;
}
.no-news-row span {
    display: inline-block;
    background: rgba(0,0,0,0.05);
    border-radius: 3px;
    padding: 1px 5px;
    margin: 1px 2px;
    font-size: 10px;
}
.theme-bar {
    background: #1a1a2e;
    border-radius: 8px;
    padding: 10px 14px;
    display: flex;
    align-items: center;
    gap: 10px;
    margin-bottom: 8px;
}
.theme-label {
    font-size: 11px;
    color: #888;
    white-space: nowrap;
}
.theme-text {
    font-size: 13px;
    color: #fff;
    font-weight: 500;
    line-height: 1.4;
}
.overview-block {
    border-left: 3px solid #1a73e8;
    background: rgba(26,115,232,0.05);
    padding: 8px 12px;
    border-radius: 0 6px 6px 0;
    margin-bottom: 8px;
}
.overview-block.co-overview {
    border-left-color: #e8610a;
    background: rgba(232,97,10,0.05);
}
.overview-block-label {
    font-size: 10px;
    font-weight: 500;
    color: #1a73e8;
    text-transform: uppercase;
    letter-spacing: .04em;
    margin-bottom: 3px;
}
.overview-block-label.co { color: #e8610a; }
.overview-block-text {
    font-size: 12px;
    color: var(--color-text-secondary, #555);
    line-height: 1.6;
}
.section-divider {
    display: flex;
    align-items: center;
    gap: 8px;
    margin: 8px 0 4px;
}
.section-divider-title {
    font-size: 11px;
    font-weight: 500;
    color: var(--color-text-primary, #111);
    white-space: nowrap;
}
.section-divider-line {
    flex: 1;
    height: 0.5px;
    background: rgba(0,0,0,0.1);
}
</style>
""",
        unsafe_allow_html=True,
    )


def _render_theme_bar(theme: str) -> None:
    if not theme:
        return
    st.markdown(
        f'<div class="theme-bar">'
        f'<span class="theme-label">今日主线</span>'
        f'<span class="theme-text">{_escape_html(theme)}</span>'
        f"</div>",
        unsafe_allow_html=True,
    )


def _render_overview_block(text: str, *, is_company: bool = False) -> None:
    if not text:
        return
    co_cls = "co-overview" if is_company else ""
    label_cls = "co" if is_company else ""
    label = "公司动态概览" if is_company else "宏观概览"
    st.markdown(
        f'<div class="overview-block {co_cls}">'
        f'<div class="overview-block-label {label_cls}">{label}</div>'
        f'<div class="overview-block-text">{_escape_html(text)}</div>'
        f"</div>",
        unsafe_allow_html=True,
    )


def _render_section_divider(title: str) -> None:
    st.markdown(
        f'<div class="section-divider">'
        f'<span class="section-divider-title">{_escape_html(title)}</span>'
        f'<span class="section-divider-line"></span>'
        f"</div>",
        unsafe_allow_html=True,
    )


def _render_macro_compact_item(item: dict) -> str:
    """返回单条宏观新闻的 HTML 字符串。"""
    title = _escape_html(item.get("title") or "—")
    summary = _escape_html(item.get("summary") or "")
    source = _escape_html(item.get("source") or "")
    url = (item.get("source_url") or "").strip()
    pub = item.get("published_at_ny") or item.get("published_at") or ""
    imp = item.get("importance_score") or ""
    badge = format_ny_badge(pub) if pub else ""

    meta_parts = []
    if badge:
        meta_parts.append(_escape_html(badge))
    star_str = f"★{imp}" if imp else ""
    if star_str:
        meta_parts.append(star_str)
    if source:
        meta_parts.append(source)
    if url:
        meta_parts.append(f'<a href="{url}" target="_blank">→ 原文</a>')
    meta_html = " · ".join(meta_parts)

    return (
        f'<div class="news-compact">'
        f'<div class="news-title-row">'
        f'<span class="news-title-text">{title}</span>'
        f"</div>"
        f'<div class="news-meta-row">{meta_html}</div>'
        f'<div class="news-summary-text">{summary}</div>'
        f"</div>"
    )


def _render_macro_two_col(macro_items: list) -> None:
    """按地区两列渲染宏观新闻。"""
    from collections import defaultdict

    region_map = defaultdict(list)
    for item in macro_items:
        r = (item.get("region") or "Global").strip()
        region_map[r].append(item)

    left_regions = ["North America", "Europe"]
    right_regions = ["Middle East", "Asia", "Global"]

    def col_html(regions: list[str]) -> str:
        html = '<div class="region-col">'
        for region in regions:
            items = region_map.get(region, [])
            if not items:
                continue
            pill_cls = REGION_PILL_CLASS.get(region, "rp-gl")
            html += f'<span class="region-pill {pill_cls}">{_escape_html(region)}</span>'
            items_sorted = sorted(items, key=lambda x: -(x.get("importance_score") or 0))
            for item in items_sorted:
                html += _render_macro_compact_item(item)
        html += "</div>"
        return html

    left_html = col_html(left_regions)
    right_html = col_html(right_regions)
    left_has = any(region_map.get(r) for r in left_regions)
    right_has = any(region_map.get(r) for r in right_regions)

    if left_has and right_has:
        st.markdown(
            f'<div class="news-two-col">{left_html}{right_html}</div>',
            unsafe_allow_html=True,
        )
    elif left_has:
        st.markdown(left_html, unsafe_allow_html=True)
    else:
        st.markdown(right_html, unsafe_allow_html=True)


def _render_company_table(company_items: list) -> None:
    """表格式渲染公司新闻。"""
    if not company_items:
        st.caption("暂无公司重点动态。")
        return
    items_sorted = sorted(company_items, key=lambda x: -(x.get("importance_score") or 0))
    rows_html = ""
    for item in items_sorted:
        ticker = _escape_html((item.get("ticker") or "").strip().upper() or "—")
        title = _escape_html(item.get("title") or "—")
        summary = _escape_html(item.get("summary") or "")
        source = _escape_html(item.get("source") or "")
        url = (item.get("source_url") or "").strip()
        pub = item.get("published_at_ny") or ""
        imp = item.get("importance_score") or ""
        event_type = _escape_html(item.get("event_type") or "other")
        badge = format_ny_badge(pub) if pub else ""

        meta_parts = []
        if badge:
            meta_parts.append(_escape_html(badge))
        if source:
            meta_parts.append(source)
        if url:
            meta_parts.append(f'<a href="{url}" target="_blank">→ 原文</a>')
        meta_html = " · ".join(meta_parts)
        star_str = f"★{imp}" if imp else ""

        rows_html += (
            f"<tr>"
            f'<td style="width:54px;padding-top:8px">'
            f'<span class="ticker-badge">{ticker}</span>'
            f"</td>"
            f"<td>"
            f'<div class="co-title-row">'
            f'<span class="event-tag">{event_type}</span>'
            f'<span class="co-star">{star_str}</span>'
            f'<span class="co-title-text">{title}</span>'
            f"</div>"
            f'<div class="co-summary-text">{summary}</div>'
            f'<div class="co-meta-text">{meta_html}</div>'
            f"</td>"
            f"</tr>"
        )
    st.markdown(
        f'<table class="co-table">{rows_html}</table>',
        unsafe_allow_html=True,
    )


def _render_no_news_tickers(tickers: list) -> None:
    if not tickers:
        return
    tags = "".join(f"<span>{_escape_html(t)}</span>" for t in tickers)
    st.markdown(
        f'<div class="no-news-row">本期无实质动态：{tags}</div>',
        unsafe_allow_html=True,
    )


_inject_styles()


with st.spinner("拉取隔夜速递、昨日总结与晨报…"):
    overnight: dict = {}
    yesterday_doc: dict = {}
    try:
        overnight = _load_overnight(
            BACKEND_BASE, st.session_state.brief_cache_key, sector
        )
    except Exception as e:
        st.warning(f"隔夜速递加载失败：{format_api_error(e)}")
    try:
        yesterday_doc = _load_yesterday_summary(
            BACKEND_BASE, st.session_state.brief_cache_key, sector
        )
    except Exception as e:
        st.warning(f"昨日总结加载失败：{format_api_error(e)}")
    # try:
    #     data = _load_morning_brief(BACKEND_BASE, st.session_state.brief_cache_key)
    # except Exception as e:
    #     full = notify_api_failure(e, prefix="晨报：")
    #     st.error(f"晨报加载失败：{full}")
    #     st.stop()

_export_md = build_morning_brief_news_markdown(
    sector,
    overnight if overnight else None,
    yesterday_doc if yesterday_doc else None,
)
st.download_button(
    "导出新闻版块 Markdown",
    data=_export_md.encode("utf-8"),
    file_name=f"morning_brief_export_{sector}.md",
    mime="text/markdown; charset=utf-8",
    disabled=not (overnight or yesterday_doc),
    help="与当前板块、当前刷新缓存一致的隔夜速递 + 昨日总结（.md）",
)

# --- 晨报主体（morning-brief）临时屏蔽 ---
# briefing / top_news / clusters / macro / company_news_list 相关渲染已临时注释。
# 仅保留隔夜速递与昨日总结展示。

st.subheader(f"隔夜速递 — {sector}")
if not overnight:
    st.caption("隔夜速递未加载。")
else:
    ows = (overnight.get("overnight_summary") or "").strip()
    if ows:
        st.success(ows)
    wns = overnight.get("window_start_ny")
    wne = overnight.get("window_end_ny")
    if wns and wne:
        st.caption(f"时间窗（NY）：{wns} → {wne}")

    _render_theme_bar(overnight.get("macro_today_theme") or "")
    _render_overview_block(overnight.get("macro_summary") or "")

    o_macro = overnight.get("macro_news") or []
    if o_macro:
        _render_section_divider("宏观新闻")
        _render_macro_two_col(o_macro)
    else:
        st.caption("暂无宏观隔夜重点。")

    _render_section_divider("公司动态")
    _render_overview_block(overnight.get("company_summary") or "", is_company=True)
    _render_company_table(overnight.get("company_news") or [])
    _render_no_news_tickers(overnight.get("no_news_tickers") or [])

with st.expander(f"昨日总结 — {sector}", expanded=False):
    if not yesterday_doc:
        st.caption("昨日总结未加载（见上方警告）。")
    else:
        yws = yesterday_doc.get("window_start_ny")
        ywe = yesterday_doc.get("window_end_ny")
        if yws and ywe:
            st.caption(f"时间窗（NY）：{yws} → {ywe}")
        yn = yesterday_doc.get("articles_in_window")
        if isinstance(yn, int):
            st.caption(f"窗口内新闻条数：{yn}")

        _render_theme_bar(yesterday_doc.get("macro_today_theme") or "")
        _render_overview_block(yesterday_doc.get("macro_summary") or "")

        y_macro = yesterday_doc.get("macro_news") or []
        if y_macro:
            _render_section_divider("宏观新闻")
            _render_macro_two_col(y_macro)
        else:
            st.caption("昨日暂无宏观重点。")

        _render_section_divider("公司动态")
        _render_overview_block(yesterday_doc.get("company_summary") or "", is_company=True)
        _render_company_table(yesterday_doc.get("company_news") or [])
        _render_no_news_tickers(yesterday_doc.get("no_news_tickers") or [])

st.divider()

# --- 晨报主体（morning-brief）临时屏蔽 ---
# st.caption(
#     "**宏观新闻**：多源市场 RSS 按序回退（路透 / 彭博 / WSJ / FT / Yahoo）"
#     "，失败时用 6 小时内磁盘缓存，仍空再合并站内 RSS_FEEDS。"
# )
#
# # 后端 ``data_source_label`` 含 Benzinga/Finnhub API 配置说明，易在宏观/公司新闻区底部形成冗长提示，故不展示。
# # dsl = (data.get("data_source_label") or "").strip()
# # if dsl:
# #     st.caption(f"**整体来源**：{dsl}")
# pn = (data.get("provenance_note") or "").strip()
# if pn:
#     st.info(pn)
#
# st.subheader("宏观新闻")
# if not main_macro:
#     st.caption(
#         "暂无（列表为空、与今日必读标题去重，或重要性均 ≤3 已收入底部「背景资料」）。"
#     )
# else:
#     for i, item in enumerate(main_macro):
#         _render_news_card(item, f"macro_{i}", backend=BACKEND_BASE, show_financial=False)
#         if i < len(main_macro) - 1:
#             st.divider()
#
# st.subheader("公司新闻")
# if not company_news_list:
#     st.caption("暂无公司新闻。")
# elif not main_company:
#     st.caption(
#         "主区域暂无：当前公司新闻的重要性均 ≤3，已收入下方「背景资料（低分新闻）」。"
#     )
# else:
#     for i, item in enumerate(main_company):
#         _render_news_card(
#             item,
#             f"co_{i}",
#             backend=BACKEND_BASE,
#             show_financial=True,
#         )
#         if i < len(main_company) - 1:
#             st.divider()
#
# if low_items:
#     with st.expander(
#         f"📦 背景资料（低分新闻，共 {len(low_items)} 条）",
#         expanded=False,
#     ):
#         for li, item in enumerate(low_items):
#             _render_news_card(
#                 item,
#                 f"low_{li}",
#                 backend=BACKEND_BASE,
#                 show_financial=bool(item.get("matched_tickers")),
#             )
#             if li < len(low_items) - 1:
#                 st.divider()
