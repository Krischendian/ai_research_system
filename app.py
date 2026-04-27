"""Streamlit 入口：侧边栏多页面导航。"""
from pathlib import Path

import streamlit as st

_ROOT = Path(__file__).resolve().parent

st.set_page_config(page_title="AI 投研系统", layout="wide")

st.sidebar.title("AI 投研系统")

pg = st.navigation(
    [
       # st.Page(
        #    str(_ROOT / "frontend" / "pages" / "01_DeepDive.py"),
         #   title="深度分析",
          #  icon="🔬",
       # ),
       # st.Page(
        #    str(_ROOT / "frontend" / "pages" / "02_MorningBrief.py"),
         #   title="自动化晨报",
         #   icon="🌅",
        #),
        st.Page(
            str(_ROOT / "frontend" / "pages" / "04_SectorReport.py"),
            title="行业监控报告",
            icon="📋",
        ),
        #st.Page(
         #   str(_ROOT / "frontend" / "pages" / "03_Search.py"),
          #  title="全局搜索",
           # icon="🔎",
        #),
    ],
    position="sidebar",
)
pg.run()

_err = st.session_state.pop("_global_toast_error", None)
if _err:
    st.toast(_err, icon="❌")
