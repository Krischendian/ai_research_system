"""深度分析：财务数据 + 业务画像。"""
from __future__ import annotations

import streamlit as st
import pandas as pd
import requests

from frontend.streamlit_helpers import notify_api_failure

BACKEND_BASE = "http://127.0.0.1:8000"

st.title("深度分析")

if "fin_error" not in st.session_state:
    st.session_state.fin_error = None
if "fin_payload" not in st.session_state:
    st.session_state.fin_payload = None
if "profile_error" not in st.session_state:
    st.session_state.profile_error = None
if "profile_payload" not in st.session_state:
    st.session_state.profile_payload = None
if "queried" not in st.session_state:
    st.session_state.queried = False

ticker = st.text_input("股票代码", value="AAPL")


def _fmt_usd(v: float | None) -> str:
    if v is None:
        return "—"
    return f"{v:,.0f}"


def _fmt_ratio(v: float | None) -> str:
    if v is None:
        return "—"
    return f"{v * 100:.2f}%"


if st.button("查询"):
    st.session_state.fin_error = None
    st.session_state.fin_payload = None
    st.session_state.profile_error = None
    st.session_state.profile_payload = None
    st.session_state.queried = True

    with st.spinner("加载中..."):
        sym = ticker.strip().upper()
        if not sym:
            msg = "请输入股票代码"
            st.session_state.fin_error = msg
            st.session_state.profile_error = msg
            notify_api_failure(ValueError(msg), prefix="")
        else:
            try:
                r = requests.get(
                    f"{BACKEND_BASE}/api/v1/companies/{sym}/financials",
                    timeout=60,
                )
                r.raise_for_status()
                st.session_state.fin_payload = r.json()
            except Exception as e:
                st.session_state.fin_error = notify_api_failure(
                    e, prefix="财务数据："
                )

            try:
                r2 = requests.get(
                    f"{BACKEND_BASE}/api/v1/companies/{sym}/business-profile",
                    timeout=120,
                )
                r2.raise_for_status()
                st.session_state.profile_payload = r2.json()
            except Exception as e:
                st.session_state.profile_error = notify_api_failure(
                    e, prefix="业务画像："
                )

if st.session_state.queried:
    st.subheader("财务数据")
    if st.session_state.fin_error:
        st.error(f"财务数据加载失败：{st.session_state.fin_error}")
    elif st.session_state.fin_payload is not None:
        p = st.session_state.fin_payload
        st.caption(
            f"标的 `{p.get('ticker')}` · 数据时间 `{p.get('last_updated', '')}`"
        )
        dsl = (p.get("data_source_label") or "").strip()
        if dsl:
            st.caption(f"**数据溯源**：{dsl}")
        purl = p.get("primary_source_url")
        if purl:
            st.markdown(f"[→ Yahoo Finance 行情与报表入口（原文数据）]({purl})")
        rows = p.get("financials") or []
        if not rows:
            st.info(
                "暂无财务数据，请先在后端抓取并入库（例如运行 tests/test_financials.py）。"
            )
        else:
            display_rows = []
            for row in rows:
                display_rows.append(
                    {
                        "年份": row.get("year"),
                        "营收（美元）": _fmt_usd(row.get("revenue")),
                        "EBITDA（美元）": _fmt_usd(row.get("ebitda")),
                        "资本支出（美元）": _fmt_usd(row.get("capex")),
                        "毛利率": _fmt_ratio(row.get("gross_margin")),
                        "净负债/权益比": (
                            f"{row.get('net_debt_to_equity'):.4f}"
                            if row.get("net_debt_to_equity") is not None
                            else "—"
                        ),
                    }
                )
            df = pd.DataFrame(display_rows)
            st.dataframe(df, use_container_width=True, hide_index=True)

    st.subheader("业务画像")
    if st.session_state.profile_error:
        st.error(f"业务画像加载失败：{st.session_state.profile_error}")
    elif st.session_state.profile_payload is not None:
        prof = st.session_state.profile_payload
        st.caption(
            f"标的 `{prof.get('ticker')}` · 更新于 `{prof.get('last_updated', '')}`"
        )
        psl = (prof.get("data_source_label") or "").strip()
        if psl:
            st.caption(f"**数据溯源**：{psl}")
        pub = prof.get("primary_source_url")
        if pub:
            st.markdown(f"[→ SEC EDGAR 检索（法定披露入口）]({pub})")
        st.markdown(prof.get("core_business") or "")

        seg = prof.get("revenue_by_segment") or []
        geo = prof.get("revenue_by_geography") or []

        c1, c2 = st.columns(2)
        with c1:
            st.markdown("**业务线营收占比**")
            if seg:
                df_s = pd.DataFrame(
                    [
                        {"业务线": s.get("segment_name"), "占比": s.get("percentage")}
                        for s in seg
                    ]
                )
                st.dataframe(df_s, use_container_width=True, hide_index=True)
            else:
                st.caption("无数据")
        with c2:
            st.markdown("**地区营收占比**")
            if geo:
                df_g = pd.DataFrame(
                    [
                        {"地区": s.get("segment_name"), "占比": s.get("percentage")}
                        for s in geo
                    ]
                )
                st.dataframe(df_g, use_container_width=True, hide_index=True)
            else:
                st.caption("无数据")
