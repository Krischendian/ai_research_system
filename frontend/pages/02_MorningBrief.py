"""自动化晨报：RSS + 后端 LLM 摘要。"""
from __future__ import annotations

import sys
from pathlib import Path

_fe_root = Path(__file__).resolve().parent.parent.parent
if str(_fe_root) not in sys.path:
    sys.path.insert(0, str(_fe_root))

import requests
import streamlit as st

from frontend.streamlit_helpers import format_api_error, notify_api_failure

BACKEND_BASE = "http://127.0.0.1:8000"

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

st.subheader("隔夜速递")
if not overnight:
    st.caption("隔夜速递未加载（若上方有警告，多为网络或 OpenAI 配置问题）。")
else:
    ows = (overnight.get("summary") or "").strip()
    if ows:
        st.success(ows)
    wns = overnight.get("window_start_ny")
    wne = overnight.get("window_end_ny")
    if wns and wne:
        st.caption(f"时间窗（NY）：{wns} → {wne}")
    opn = (overnight.get("provenance_note") or "").strip()
    if opn:
        st.caption(opn)
    onews = overnight.get("news_list") or []
    if not onews:
        st.caption("本窗口内暂无带有效发布时间的 RSS 条目（或当前批次未命中）。")
    else:
        with st.expander(f"本窗口内 RSS 条目（{len(onews)} 条）", expanded=False):
            for j, item in enumerate(onews):
                ot = item.get("title") or "—"
                osrc = item.get("source") or "—"
                opub = (item.get("published_at_ny") or "").strip()
                ourl = (item.get("source_url") or "").strip()
                otags = item.get("matched_tickers") or []
                st.markdown(f"**{ot}**")
                st.caption(f"来源：{osrc}" + (f" · {opub}" if opub else ""))
                if otags:
                    st.caption("🏷️ " + " · ".join(str(t).upper() for t in otags))
                if ourl:
                    st.markdown(f"[→ 阅读原文]({ourl})")
                st.write(item.get("summary") or "—")
                if j < len(onews) - 1:
                    st.divider()

with st.expander("昨日总结", expanded=False):
    if not yesterday_doc:
        st.caption("昨日总结未加载（见上方警告）。")
    else:
        ymd = (yesterday_doc.get("markdown") or "").strip()
        if ymd:
            st.markdown(ymd)
        yws = yesterday_doc.get("window_start_ny")
        ywe = yesterday_doc.get("window_end_ny")
        if yws and ywe:
            st.caption(f"时间窗（NY，昨日全天）：{yws} → {ywe}")
        yn = yesterday_doc.get("articles_in_window")
        if isinstance(yn, int):
            st.caption(f"本窗口内用于归类的 RSS 条数：**{yn}**")
        ypn = (yesterday_doc.get("provenance_note") or "").strip()
        if ypn:
            st.caption(ypn)

st.divider()

dsl = (data.get("data_source_label") or "").strip()
if dsl:
    st.caption(f"**整体来源**：{dsl}")
pn = (data.get("provenance_note") or "").strip()
if pn:
    st.info(pn)

macro = data.get("macro_news") or []
company = data.get("company_news") or []


def _render_news_list(items: list) -> None:
    for i, item in enumerate(items):
        title = item.get("title") or "—"
        summary = item.get("summary") or "—"
        source = item.get("source") or "—"
        surl = (item.get("source_url") or "").strip()
        tags = item.get("matched_tickers") or []
        st.markdown(f"**{title}**")
        st.caption(f"来源：{source}")
        if tags:
            st.caption("🏷️ " + " · ".join(str(t).upper() for t in tags))
        if surl:
            st.markdown(f"[→ 阅读原文 / 报道页面]({surl})")
        else:
            st.caption("*暂无匹配的原文链接（请以来源标识检索核对）*")
        st.write(summary)
        if i < len(items) - 1:
            st.divider()


st.subheader("宏观新闻")
if not macro:
    st.caption("暂无")
else:
    _render_news_list(macro)

st.subheader("公司新闻")
if not company:
    st.info(
        "**暂无**：本批里没有任何一条在正文（标题+摘要+提要）中命中你「监控池」里的 ticker / 公司关键词。"
        " 之前若全是地缘、油价类头条，属于正常情况。"
        " 后端已**优先抓取** Bloomberg 科技/行业 + TechCrunch，请**重启 API** 后在上方点 **「刷新」**。"
    )
    st.caption(
        "若尚未写入监控公司，可在项目根执行：`PYTHONPATH=src python -c "
        "\"from research_automation.core.company_manager import seed_default_tech_companies; "
        "seed_default_tech_companies()\"`"
    )
else:
    _render_news_list(company)
