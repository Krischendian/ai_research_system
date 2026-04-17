"""全局关键词搜索：10-K 缓存、晨报新闻 JSON、电话会逐字稿；RAG 问答。"""
from __future__ import annotations

import re
import sys
from pathlib import Path
from typing import Any, Literal

import requests
import streamlit as st

_fe_root = Path(__file__).resolve().parent.parent.parent
if str(_fe_root) not in sys.path:
    sys.path.insert(0, str(_fe_root))

from frontend.morning_brief_helpers import deep_dive_switch_page
from frontend.streamlit_helpers import notify_api_failure

BACKEND_BASE = "http://127.0.0.1:8000"

ResultKind = Literal["10k", "news", "earnings", "other"]


def _result_kind_and_tag(title: str) -> tuple[ResultKind, str]:
    t = (title or "").strip()
    if t.startswith("10-K "):
        m = re.match(r"10-K\s+(.+?)\s*\(\d{4}\)", t)
        item = (m.group(1).strip() if m else "").strip() or "10-K"
        return "10k", f"10-K · {item}"
    if t.startswith("News:"):
        return "news", "新闻"
    if t.startswith("Earnings Call"):
        return "earnings", "电话会"
    return "other", "其他"


def _parse_earnings_title(title: str) -> tuple[str, str] | None:
    """``Earnings Call 2024Q4 · AAPL`` → (ticker, quarter)。"""
    m = re.match(
        r"^Earnings Call\s+(\d{4}Q[1-4])\s*·\s*([A-Z0-9.]+)\s*$",
        (title or "").strip(),
        re.I,
    )
    if not m:
        return None
    return m.group(2).strip().upper(), m.group(1).upper()


def _render_result_card(row: dict[str, Any], idx: int) -> None:
    title = str(row.get("title") or "").strip() or "（无标题）"
    snippet = str(row.get("snippet") or "").strip() or "—"
    source_url = row.get("source_url")
    url = str(source_url).strip() if source_url else ""
    kind, source_tag = _result_kind_and_tag(title)

    st.markdown(f"**{title}**")
    st.caption(f"来源：{source_tag}")
    st.markdown(snippet)

    if kind == "10k" and url:
        st.link_button("查看原文", url, key=f"search_open_sec_{idx}")
    elif kind == "news" and url:
        st.link_button("查看原文", url, key=f"search_open_news_{idx}")
    elif kind == "earnings":
        parsed = _parse_earnings_title(title)
        if parsed:
            sym, qlabel = parsed
            if st.button("查看原文（深度分析）", key=f"search_open_ec_{idx}"):
                st.session_state["deep_dive_prefill_ticker"] = sym
                st.session_state["deep_dive_prefill_quarter"] = qlabel
                st.session_state["deep_dive_auto_query"] = True
                st.switch_page(deep_dive_switch_page(_fe_root))
        elif url:
            st.link_button("查看原文", url, key=f"search_open_ec_url_{idx}")
        else:
            st.caption("暂无原文链接")
    elif url:
        st.link_button("查看原文", url, key=f"search_open_generic_{idx}")
    else:
        st.caption("暂无原文链接")

    st.divider()


def _render_rag_source(src: dict[str, Any], idx: int) -> None:
    label = str(src.get("label") or "").strip() or "来源"
    title = str(src.get("title") or "").strip()
    summary = str(src.get("summary") or "").strip() or "—"
    url = src.get("url")
    url_s = str(url).strip() if url else ""
    st.markdown(f"**[{idx}] {label}**")
    if title:
        st.caption(title)
    st.markdown(summary)
    if url_s:
        st.link_button("打开链接", url_s, key=f"rag_src_url_{idx}")
    st.divider()


st.title("全局搜索")

mode = st.radio(
    "功能",
    ["关键词搜索", "AI 问答"],
    horizontal=True,
    key="search_page_mode",
)

if mode == "关键词搜索":
    q = st.text_input("搜索关键词", key="global_search_query", placeholder="输入关键词…")
    run = st.button("搜索", type="primary")

    if run:
        needle = (q or "").strip()
        if not needle:
            st.warning("请输入关键词。")
        else:
            with st.spinner("正在搜索…"):
                try:
                    r = requests.post(
                        f"{BACKEND_BASE}/api/v1/search",
                        json={"query": needle, "limit": 20},
                        timeout=120,
                    )
                    r.raise_for_status()
                    payload = r.json()
                except Exception as e:
                    st.error(notify_api_failure(e, prefix="搜索失败："))
                    st.stop()
            results = payload.get("results") if isinstance(payload, dict) else None
            if not isinstance(results, list):
                results = []
            st.session_state["global_search_last_results"] = results
            st.session_state["global_search_last_query"] = needle
            st.session_state["_global_search_has_run"] = True

    if st.session_state.get("_global_search_has_run"):
        res = st.session_state.get("global_search_last_results", [])
        q_done = st.session_state.get("global_search_last_query") or ""
        if q_done:
            st.caption(f"关键词：`{q_done}` · 共 {len(res)} 条")
        if not res:
            st.info("未找到相关内容")
        else:
            for i, row in enumerate(res):
                if isinstance(row, dict):
                    _render_result_card(row, i)

else:
    aq = st.text_area(
        "问题",
        key="global_ask_question",
        placeholder="用自然语言提问，例如：公司最近一年的主要风险有哪些？",
        height=100,
    )
    ask_run = st.button("提问 AI", type="primary")

    if ask_run:
        qq = (aq or "").strip()
        if not qq:
            st.warning("请输入问题。")
        else:
            with st.spinner("正在检索并生成回答…"):
                try:
                    r = requests.post(
                        f"{BACKEND_BASE}/api/v1/search/ask",
                        json={"question": qq},
                        timeout=180,
                    )
                    r.raise_for_status()
                    payload = r.json()
                except Exception as e:
                    st.error(notify_api_failure(e, prefix="AI 问答失败："))
                    st.stop()
            st.session_state["global_ask_last_payload"] = payload
            st.session_state["global_ask_last_question"] = qq
            st.session_state["_global_ask_has_run"] = True

    if st.session_state.get("_global_ask_has_run"):
        pl = st.session_state.get("global_ask_last_payload")
        q_done = st.session_state.get("global_ask_last_question") or ""
        if not isinstance(pl, dict):
            pl = {}
        if q_done:
            st.caption(f"问题：`{q_done}`")
        answer = str(pl.get("answer") or "").strip()
        if answer:
            st.subheader("回答")
            st.markdown(answer)
        sources = pl.get("sources")
        if not isinstance(sources, list):
            sources = []
        with st.expander(f"引用来源（{len(sources)} 条）", expanded=False):
            if not sources:
                st.caption("无引用片段")
            else:
                for i, srow in enumerate(sources):
                    if isinstance(srow, dict):
                        _render_rag_source(srow, i)
