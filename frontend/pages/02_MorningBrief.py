"""自动化晨报：RSS + 后端 LLM 摘要。"""
from __future__ import annotations

import streamlit as st
import requests

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
def _load_morning_brief(_backend: str, _cache_key: int) -> dict:
    r = requests.get(
        f"{_backend}/api/v1/news/morning-brief",
        timeout=180,
    )
    r.raise_for_status()
    return r.json()


with st.spinner("拉取 RSS 并生成摘要，请稍候…"):
    try:
        data = _load_morning_brief(BACKEND_BASE, st.session_state.brief_cache_key)
    except Exception as e:
        full = notify_api_failure(e, prefix="晨报：")
        st.error(f"晨报加载失败：{full}")
        st.stop()

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
        st.markdown(f"**{title}**")
        st.caption(f"来源：{source}")
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
    st.caption("暂无")
else:
    _render_news_list(company)
