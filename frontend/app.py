"""Streamlit 主入口。"""
from __future__ import annotations

import sys
from pathlib import Path

# 必须把项目根目录放进 sys.path，否则 pages 里 `from frontend.xxx` 会报
# ModuleNotFoundError: No module named 'frontend'
_ROOT = Path(__file__).resolve().parent.parent
if str(_ROOT) not in sys.path:
    sys.path.insert(0, str(_ROOT))

import streamlit as st

st.title("AI 投研分析系统 POC")
st.markdown(
    "请在左侧边栏选择页面：\n"
    "- **DeepDive**：深度分析（财务、业务画像等）\n"
    "- **MorningBrief**：自动化晨报与隔夜速递\n"
)
