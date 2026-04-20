"""每日新闻简报：宏观（Bloomberg RSS）+ 公司新闻（Benzinga），按sector分类，LLM提炼要点。"""
from __future__ import annotations

import sys
from pathlib import Path

_fe_root = Path(__file__).resolve().parent.parent.parent
_src = _fe_root / "src"
for p in (_fe_root, _src):
    if str(p) not in sys.path:
        sys.path.insert(0, str(p))

import streamlit as st
from datetime import date, timedelta
from dotenv import load_dotenv
from research_automation.core.company_manager import list_companies
from research_automation.core.database import get_connection, init_db
from research_automation.services.daily_brief_service import generate_daily_brief

load_dotenv(_fe_root / ".env", override=False)

st.title("每日新闻简报")
st.caption("宏观：Bloomberg RSS（sector相关过滤）｜公司：Benzinga API｜摘要：Claude")

force_refresh = st.sidebar.checkbox("强制刷新简报（忽略缓存）", value=False)
use_custom_date = st.sidebar.checkbox("指定日期（测试用）", value=False)
if use_custom_date:
    target_date = st.sidebar.date_input(
        "选择纽约日期",
        value=date.today() - timedelta(days=1),
        max_value=date.today(),
    )
else:
    target_date = None


def _get_sectors() -> list[str]:
    conn = get_connection()
    try:
        init_db(conn)
        cur = conn.execute(
            "SELECT DISTINCT sector FROM companies WHERE is_active=1 AND TRIM(sector)!='' ORDER BY sector"
        )
        return [r[0] for r in cur.fetchall()]
    finally:
        conn.close()

sectors = _get_sectors()
if not sectors:
    st.warning("数据库中无活跃sector。")
    st.stop()

col1, col2 = st.columns([3, 1])
with col1:
    sector = st.selectbox("选择 Sector", sectors)
with col2:
    gen = st.button("生成简报", type="primary")

if sector:
    companies = list_companies(sector=sector, active_only=True)
    tickers = [c.ticker for c in companies]
    st.caption(f"共 {len(tickers)} 家公司：{', '.join(tickers[:10])}{'...' if len(tickers)>10 else ''}")

# 缓存key
cache_key = f"daily_brief_{sector}"

if gen:
    with st.spinner("正在拉取新闻并生成摘要（约30-60秒）..."):
        try:
            brief = generate_daily_brief(
                sector, tickers, force_refresh=force_refresh, target_date=target_date
            )
            st.session_state[cache_key] = brief
        except Exception as e:
            st.error(f"生成失败：{e}")

brief = st.session_state.get(cache_key)
if brief:
    st.success(f"已生成：**{sector}** 每日简报")
    st.download_button(
        "下载 Markdown",
        data=brief.encode("utf-8"),
        file_name=f"daily_brief_{sector}.md",
        mime="text/markdown",
    )
    st.markdown(brief, unsafe_allow_html=True)
