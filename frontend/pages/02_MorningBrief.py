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
    deep_dive_switch_page,
    extract_topic_tags,
    fetch_financial_snippet,
    format_ny_badge,
    item_importance,
    sentiment_bg_color,
    sentiment_for_item,
    sentiment_from_text,
    title_html_block,
)
from frontend.streamlit_helpers import format_api_error, notify_api_failure

BACKEND_BASE = "http://127.0.0.1:8000"
_PROJECT_ROOT = _fe_root

REGION_COLOR = {
    "North America": "#1a73e8",
    "Europe": "#34a853",
    "Middle East": "#fbbc04",
    "Asia": "#ea4335",
    "Global": "#9e9e9e",
}

EVENT_TYPE_LABEL = {
    "earnings": "📊 财务业绩",
    "partnership": "🤝 新合作",
    "ma": "🔀 并购",
    "buyback": "💰 回购",
    "insider_trade": "👤 Insider交易",
    "management": "🎙️ 管理层",
    "research": "🔬 研究/评级",
    "other": "📌 其他",
}

if "brief_cache_key" not in st.session_state:
    st.session_state.brief_cache_key = 0

st.title("自动化晨报")

c1, c2 = st.columns([1, 5])
with c1:
    if st.button("刷新"):
        st.session_state.brief_cache_key += 1


@st.cache_data(ttl=120)
def _load_overnight(_backend: str, _cache_key: int) -> dict:
    r = requests.get(
        f"{_backend}/api/v1/news/overnight",
        timeout=120,
    )
    r.raise_for_status()
    return r.json()


@st.cache_data(ttl=300)
def _load_yesterday_summary(_backend: str, _cache_key: int) -> dict:
    r = requests.get(
        f"{_backend}/api/v1/news/yesterday-summary",
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


def _sentiment_cn(s: str) -> str:
    return {"positive": "正面", "negative": "负面", "neutral": "中性"}.get(
        s, "中性"
    )


def _render_news_card(
    item: dict,
    key_prefix: str,
    *,
    backend: str,
    show_financial: bool = False,
) -> None:
    title = item.get("title") or "—"
    summary = item.get("summary") or "—"
    source = item.get("source") or "—"
    surl = (item.get("source_url") or "").strip()
    pub = item.get("published_at")
    sent = sentiment_for_item(item, title, summary)
    bg = sentiment_bg_color(sent)
    st.markdown(title_html_block(title, bg), unsafe_allow_html=True)
    topic_tags = extract_topic_tags(title, summary)
    if topic_tags:
        st.markdown(
            '<p style="font-size:0.78rem;color:#666;margin:0 0 6px 0;">'
            + " ".join(_escape_html(t) for t in topic_tags)
            + "</p>",
            unsafe_allow_html=True,
        )
    badge = format_ny_badge(pub)
    cap = f"来源：{source}"
    if badge:
        cap = f"{badge} · {cap}"
    st.caption(cap)
    _raw = item.get("matched_tickers") or []
    _seen: set[str] = set()
    tickers: list[str] = []
    for t in _raw:
        sym_u = str(t).strip().upper()
        if not sym_u or sym_u in _seen:
            continue
        _seen.add(sym_u)
        tickers.append(sym_u)
    fin_line = ""
    if show_financial and tickers:
        fin_line = fetch_financial_snippet(backend, tickers[0])
    if fin_line:
        st.caption(fin_line)
    if tickers:
        n = len(tickers)
        btn_cols = st.columns(min(n, 6))
        for bi, sym_u in enumerate(tickers[:6]):
            with btn_cols[bi]:
                if st.button(
                    "📊 深度分析",
                    key=f"{key_prefix}_dd_{sym_u}_{bi}",
                    help=f"跳转深度分析并查询 {sym_u}",
                ):
                    st.session_state["deep_dive_prefill_ticker"] = sym_u
                    st.session_state["deep_dive_auto_query"] = True
                    st.switch_page(deep_dive_switch_page(_PROJECT_ROOT))
    if surl:
        st.markdown(f"[→ 阅读原文 / 报道页面]({surl})")
    else:
        st.caption("*暂无匹配的原文链接（请以来源标识检索核对）*")
    st.write(summary)


def _render_overnight_item(item: dict, key_prefix: str) -> None:
    title = item.get("title") or "—"
    summary = item.get("summary") or "—"
    source = item.get("source") or "—"
    opub = (item.get("published_at_ny") or item.get("published_at") or "").strip()
    ourl = (item.get("source_url") or "").strip()
    otags = item.get("matched_tickers") or []
    sent = sentiment_for_item(item, title, summary)
    bg = sentiment_bg_color(sent)
    st.markdown(title_html_block(title, bg), unsafe_allow_html=True)
    topic_tags = extract_topic_tags(title, summary)
    if topic_tags:
        st.markdown(
            '<p style="font-size:0.78rem;color:#666;margin:0 0 6px 0;">'
            + " ".join(_escape_html(t) for t in topic_tags)
            + "</p>",
            unsafe_allow_html=True,
        )
    badge = format_ny_badge(opub) if opub else ""
    cap = f"来源：{source}"
    if badge:
        cap = f"{badge} · {cap}"
    st.caption(cap)
    if otags:
        st.caption("🏷️ " + " · ".join(str(t).upper() for t in otags))
    if ourl:
        st.markdown(f"[→ 阅读原文]({ourl})")
    st.write(summary)


def _render_region_badge(region: str) -> str:
    region_txt = (region or "Global").strip() or "Global"
    color = REGION_COLOR.get(region_txt, REGION_COLOR["Global"])
    return (
        f'<span style="display:inline-block;padding:2px 8px;border-radius:999px;'
        f'background:{color};color:white;font-size:0.78rem;margin-right:6px;">'
        f'{_escape_html(region_txt)}</span>'
    )


def _render_event_badge(event_type: str) -> str:
    key = (event_type or "other").strip()
    label = EVENT_TYPE_LABEL.get(key, EVENT_TYPE_LABEL["other"])
    return (
        '<span style="display:inline-block;padding:2px 8px;border-radius:999px;'
        'background:#5f6368;color:white;font-size:0.78rem;margin-right:6px;">'
        f'{_escape_html(label)}</span>'
    )


def _render_structured_macro_item(item: dict, key_prefix: str) -> None:
    title = item.get("title") or "—"
    summary = item.get("summary") or "—"
    source = item.get("source") or "—"
    source_url = (item.get("source_url") or "").strip()
    published = (item.get("published_at_ny") or "").strip()
    region = item.get("region") or "Global"
    imp = item.get("importance_score")
    st.markdown(title_html_block(title, "#f3f7ff"), unsafe_allow_html=True)
    st.markdown(_render_region_badge(region), unsafe_allow_html=True)
    cap = f"来源：{source}"
    if isinstance(imp, int):
        cap = f"★{imp} · {cap}"
    if published:
        cap = f"{format_ny_badge(published)} · {cap}"
    st.caption(cap)
    if source_url:
        st.markdown(f"[→ 阅读原文]({source_url})")
    st.write(summary)


def _render_structured_company_item(item: dict, key_prefix: str) -> None:
    ticker = (item.get("ticker") or "").strip().upper() or "—"
    title = item.get("title") or "—"
    summary = item.get("summary") or "—"
    source = item.get("source") or "—"
    source_url = (item.get("source_url") or "").strip()
    published = (item.get("published_at_ny") or "").strip()
    event_type = item.get("event_type") or "other"
    imp = item.get("importance_score")
    st.markdown(title_html_block(title, "#fff7f2"), unsafe_allow_html=True)
    st.markdown(
        f'<span style="display:inline-block;padding:2px 8px;border-radius:999px;'
        f'background:#202124;color:white;font-size:0.78rem;margin-right:6px;">'
        f'{_escape_html(ticker)}</span>' + _render_event_badge(event_type),
        unsafe_allow_html=True,
    )
    cap = f"来源：{source}"
    if isinstance(imp, int):
        cap = f"★{imp} · {cap}"
    if published:
        cap = f"{format_ny_badge(published)} · {cap}"
    st.caption(cap)
    if source_url:
        st.markdown(f"[→ 阅读原文]({source_url})")
    st.write(summary)


def _render_cluster_block(
    cl: dict,
    backend: str,
    key_prefix: str,
    ci: int,
    *,
    show_financial_in_sources: bool,
) -> None:
    rep = (cl.get("representative_title") or "—").strip()
    score = int(cl.get("importance_score") or 5)
    st_sent = sentiment_from_text(rep, "")
    sess_cn = _sentiment_cn(st_sent)
    label = f"★{score} 分 · [{sess_cn}] · {rep[:72]}{'…' if len(rep) > 72 else ''}"
    bg = sentiment_bg_color(st_sent)
    with st.expander(label, expanded=False):
        st.markdown(title_html_block(rep, bg), unsafe_allow_html=True)
        tags = extract_topic_tags(rep, "")
        if tags:
            st.caption(" ".join(tags))
        st.caption(f"聚类 ID：`{cl.get('cluster_id') or '—'}`")
        items = cl.get("news_items") or []
        for si, sub in enumerate(items):
            st.markdown(f"**信源 {si + 1}**")
            _render_news_card(
                sub,
                f"{key_prefix}_c{ci}_s{si}",
                backend=backend,
                show_financial=show_financial_in_sources,
            )
            if si < len(items) - 1:
                st.divider()


with st.spinner("拉取隔夜速递、昨日总结与晨报…"):
    overnight: dict = {}
    yesterday_doc: dict = {}
    try:
        overnight = _load_overnight(BACKEND_BASE, st.session_state.brief_cache_key)
    except Exception as e:
        st.warning(f"隔夜速递加载失败：{format_api_error(e)}")
    try:
        yesterday_doc = _load_yesterday_summary(
            BACKEND_BASE, st.session_state.brief_cache_key
        )
    except Exception as e:
        st.warning(f"昨日总结加载失败：{format_api_error(e)}")
    try:
        data = _load_morning_brief(BACKEND_BASE, st.session_state.brief_cache_key)
    except Exception as e:
        full = notify_api_failure(e, prefix="晨报：")
        st.error(f"晨报加载失败：{full}")
        st.stop()

# --- 分析师早评（置顶）---
briefing = (data.get("analyst_briefing") or "").strip()
if briefing:
    st.markdown(
        f'<div style="background:#f0f0f0;padding:14px 16px;border-radius:8px;'
        f'border-left:4px solid #888;margin-bottom:16px;">'
        f"<strong>分析师早评</strong><br/><br/>{_escape_html(briefing)}</div>",
        unsafe_allow_html=True,
    )

# --- 今日必读（重要性 ≥7，后端扁平列表）---
top_news = data.get("top_news") or []
if top_news:
    st.subheader("🔥 今日必读")
    for i, it in enumerate(top_news):
        _render_news_card(
            it,
            f"top_{i}",
            backend=BACKEND_BASE,
            show_financial=True,
        )
        if i < len(top_news) - 1:
            st.divider()
    st.divider()

# --- 主题聚类 ---
clusters = data.get("clusters") or []
if clusters:
    st.subheader("📰 主题聚类（点击展开多信源）")
    for ci, cl in enumerate(clusters):
        if isinstance(cl, dict):
            _render_cluster_block(
                cl,
                BACKEND_BASE,
                "cluster",
                ci,
                show_financial_in_sources=True,
            )
        if ci < len(clusters) - 1:
            st.divider()
    st.divider()


def _effective_importance(it: dict) -> int:
    """重要性：有 ``importance_score`` 则用其值，否则默认 5（与后端聚类缺省一致）。"""
    sc = item_importance(it)
    return sc if sc is not None else 5


def _title_key(it: dict) -> str:
    return (it.get("title") or "").strip().lower()


top_titles = {_title_key(x) for x in top_news if _title_key(x)}

macro = data.get("macro_news") or []
company_news_list: list[dict] = list(data.get("company_news") or [])

# 主区仅 importance≥4；≤3 归入底部折叠「背景资料」（缺省分按 5 → 进主区）
main_macro = [
    x
    for x in macro
    if _effective_importance(x) >= 4 and _title_key(x) not in top_titles
]
main_company = [
    x for x in company_news_list if _effective_importance(x) >= 4
]
low_items: list[dict] = [
    x for x in macro if _effective_importance(x) <= 3
] + [x for x in company_news_list if _effective_importance(x) <= 3]

st.subheader("隔夜速递")
if not overnight:
    st.caption("隔夜速递未加载（若上方有警告，多为网络或 OpenAI 配置问题）。")
else:
    ows = (overnight.get("overnight_summary") or "").strip()
    if ows:
        st.success(ows)
    wns = overnight.get("window_start_ny")
    wne = overnight.get("window_end_ny")
    if wns and wne:
        st.caption(f"时间窗（NY）：{wns} → {wne}")
    opn = (overnight.get("provenance_note") or "").strip()
    if opn:
        st.caption(opn)
    o_macro = overnight.get("macro_news") or []
    o_company = overnight.get("company_news") or []
    st.markdown("#### 🌍 宏观")
    if not o_macro:
        st.caption("暂无宏观隔夜重点。")
    else:
        for j, item in enumerate(o_macro):
            if isinstance(item, dict):
                _render_structured_macro_item(item, f"ovm_{j}")
            if j < len(o_macro) - 1:
                st.divider()
    st.markdown("#### 🏢 公司")
    if not o_company:
        st.caption("暂无公司隔夜重点。")
    else:
        for j, item in enumerate(o_company):
            if isinstance(item, dict):
                _render_structured_company_item(item, f"ovc_{j}")
            if j < len(o_company) - 1:
                st.divider()

with st.expander("昨日总结", expanded=False):
    if not yesterday_doc:
        st.caption("昨日总结未加载（见上方警告）。")
    else:
        yws = yesterday_doc.get("window_start_ny")
        ywe = yesterday_doc.get("window_end_ny")
        if yws and ywe:
            st.caption(f"时间窗（NY，昨日全天）：{yws} → {ywe}")
        yn = yesterday_doc.get("articles_in_window")
        if isinstance(yn, int):
            st.caption(f"本窗口内用于筛选的新闻条数：**{yn}**")
        ypn = (yesterday_doc.get("provenance_note") or "").strip()
        if ypn:
            st.caption(ypn)
        y_macro = yesterday_doc.get("macro_news") or []
        y_company = yesterday_doc.get("company_news") or []
        st.markdown("#### 🌍 宏观")
        if not y_macro:
            st.caption("昨日暂无宏观重点。")
        else:
            for yi, item in enumerate(y_macro):
                if isinstance(item, dict):
                    _render_structured_macro_item(item, f"ym_{yi}")
                if yi < len(y_macro) - 1:
                    st.divider()
        st.markdown("#### 🏢 公司")
        if not y_company:
            st.caption("昨日暂无公司重点。")
        else:
            for yi, item in enumerate(y_company):
                if isinstance(item, dict):
                    _render_structured_company_item(item, f"yc_{yi}")
                if yi < len(y_company) - 1:
                    st.divider()

st.divider()

st.caption(
    "**宏观新闻**：多源市场 RSS 按序回退（路透 / 彭博 / WSJ / FT / Yahoo）"
    "，失败时用 6 小时内磁盘缓存，仍空再合并站内 RSS_FEEDS。"
)

# 后端 ``data_source_label`` 含 Benzinga/Finnhub API 配置说明，易在宏观/公司新闻区底部形成冗长提示，故不展示。
# dsl = (data.get("data_source_label") or "").strip()
# if dsl:
#     st.caption(f"**整体来源**：{dsl}")
pn = (data.get("provenance_note") or "").strip()
if pn:
    st.info(pn)

st.subheader("宏观新闻")
if not main_macro:
    st.caption(
        "暂无（列表为空、与今日必读标题去重，或重要性均 ≤3 已收入底部「背景资料」）。"
    )
else:
    for i, item in enumerate(main_macro):
        _render_news_card(item, f"macro_{i}", backend=BACKEND_BASE, show_financial=False)
        if i < len(main_macro) - 1:
            st.divider()

st.subheader("公司新闻")
if not company_news_list:
    st.caption("暂无公司新闻。")
elif not main_company:
    st.caption(
        "主区域暂无：当前公司新闻的重要性均 ≤3，已收入下方「背景资料（低分新闻）」。"
    )
else:
    for i, item in enumerate(main_company):
        _render_news_card(
            item,
            f"co_{i}",
            backend=BACKEND_BASE,
            show_financial=True,
        )
        if i < len(main_company) - 1:
            st.divider()

if low_items:
    with st.expander(
        f"📦 背景资料（低分新闻，共 {len(low_items)} 条）",
        expanded=False,
    ):
        for li, item in enumerate(low_items):
            _render_news_card(
                item,
                f"low_{li}",
                backend=BACKEND_BASE,
                show_financial=bool(item.get("matched_tickers")),
            )
            if li < len(low_items) - 1:
                st.divider()
