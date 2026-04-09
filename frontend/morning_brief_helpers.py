"""
晨报页辅助：纽约时段标签、情绪色、主题标签、财务快照（纯函数 + 可缓存请求）。
"""
from __future__ import annotations

import re
from datetime import datetime, timezone
from typing import Any, Literal, cast
from zoneinfo import ZoneInfo

import requests
import streamlit as st

_NY = ZoneInfo("America/New_York")

# 正面 / 负面关键词（中英混合，控制成本不调用 LLM）
_POS_PAT = re.compile(
    r"上涨|利好|大涨|飙升|新高|乐观|超预期|涨|beat|surge|rally|gain|upgrade|"
    r"positive|strong growth|record",
    re.I,
)
_NEG_PAT = re.compile(
    r"下跌|利空|大跌|暴跌|担忧|下调|跌|miss|plunge|slump|lawsuit|"
    r"investigation|cut guidance|weak|negative|selloff",
    re.I,
)

# (关键词元组, 标签)
_TAG_RULES: list[tuple[tuple[str, ...], str]] = [
    (("地缘", "中东", "乌克兰", "停火", "制裁", "台海", "NATO"), "#地缘政治"),
    (("供应链", "代工", "supply chain", "TSMC", "富士康"), "#供应链"),
    (("财报", "业绩", "earnings", "EPS", "guidance", "指引", "营收"), "#财报"),
    (("人工智能", "AI", "ChatGPT", "GPU", "英伟达", "Nvidia", "大模型"), "#AI"),
    (("美联储", "Fed", "鲍威尔", "加息", "降息", "利率", "CPI", "非农"), "#美联储"),
    (("原油", "油价", "OPEC", "黄金", "铜价", "大宗"), "#大宗"),
    (("关税", "贸易", "关税战", "Trump", "拜登"), "#政策"),
    (("并购", "收购", "M&A", "acquisition"), "#并购"),
]


def parse_published_to_ny(published: str | None) -> datetime | None:
    """
    将 ``published_at`` / ISO 字符串解析为 America/New_York 的 aware datetime。

    无时区信息时按 **UTC** 理解（与 API 常见 ``Z`` 一致）。
    """
    if not published or not str(published).strip():
        return None
    s = str(published).strip()
    if s.endswith("Z"):
        s = s[:-1] + "+00:00"
    try:
        dt = datetime.fromisoformat(s)
    except ValueError:
        return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(_NY)


def ny_session_label_and_clock(dt_ny: datetime) -> tuple[str, str]:
    """
    返回纽约本地时段名与 ``HH:MM``。

    盘前 04:00–09:29；盘中 09:30–16:00；盘后 16:01–20:00；其余为隔夜。
    """
    h, m = dt_ny.hour, dt_ny.minute
    mins = h * 60 + m
    hm = f"{h:02d}:{m:02d}"
    if 4 * 60 <= mins <= 9 * 60 + 29:
        return "盘前", hm
    if 9 * 60 + 30 <= mins <= 16 * 60:
        return "盘中", hm
    if 16 * 60 + 1 <= mins <= 20 * 60:
        return "盘后", hm
    return "隔夜", hm


def format_ny_badge(published: str | None) -> str:
    """展示用片段，例如 ``[盘前 07:30]（纽约）``；无法解析则返回空串。"""
    dt = parse_published_to_ny(published)
    if dt is None:
        return ""
    sess, hm = ny_session_label_and_clock(dt)
    return f"[{sess} {hm}]（纽约）"


Sentiment = Literal["positive", "negative", "neutral"]


def sentiment_from_text(title: str, summary: str) -> Sentiment:
    """基于关键词的短期影响方向（正面/负面/中性）。"""
    blob = f"{title} {summary}"
    pos = bool(_POS_PAT.search(blob))
    neg = bool(_NEG_PAT.search(blob))
    if pos and not neg:
        return "positive"
    if neg and not pos:
        return "negative"
    if pos and neg:
        return "neutral"
    return "neutral"


def sentiment_bg_color(s: Sentiment) -> str:
    """标题条背景色。"""
    if s == "positive":
        return "#e6f7e6"
    if s == "negative":
        return "#ffe6e6"
    return "#f5f5f5"


def sentiment_for_item(
    item: dict[str, Any] | None,
    title: str,
    summary: str,
) -> Sentiment:
    """优先使用 API/LLM 的 ``sentiment``，否则回退关键词启发式。"""
    if item:
        raw = item.get("sentiment")
        if isinstance(raw, str):
            k = raw.strip().lower()
            if k in ("positive", "negative", "neutral"):
                return cast(Sentiment, k)
    return sentiment_from_text(title, summary)


def extract_topic_tags(title: str, summary: str) -> list[str]:
    """从标题+摘要提取主题标签（去重保序）。"""
    blob = f"{title} {summary}"
    seen: set[str] = set()
    out: list[str] = []
    for kws, tag in _TAG_RULES:
        if any(k in blob for k in kws):
            if tag not in seen:
                seen.add(tag)
                out.append(tag)
    return out


def title_html_block(title: str, bg: str) -> str:
    """带背景色的标题 HTML（供 ``st.markdown(..., unsafe_allow_html=True)``）。"""
    safe = (
        (title or "—")
        .replace("&", "&amp;")
        .replace("<", "&lt;")
        .replace(">", "&gt;")
    )
    return (
        f'<div style="background-color:{bg};padding:8px 12px;border-radius:6px;'
        f'margin-bottom:6px;"><strong>{safe}</strong></div>'
    )


@st.cache_data(ttl=300)
def fetch_financial_snippet(backend: str, ticker: str) -> str:
    """
    拉取最新财年营收及同比变化（SEC 数据无 P/E 字段，故不展示 P/E）。

    无数据或不足两年返回空串。
    """
    sym = (ticker or "").strip().upper()
    if not sym:
        return ""
    try:
        r = requests.get(
            f"{backend}/api/v1/companies/{sym}/financials",
            timeout=30,
        )
        r.raise_for_status()
        doc = r.json()
    except Exception:
        return ""
    rows = doc.get("financials") or []
    if not rows:
        return ""
    by_year = sorted(rows, key=lambda x: int(x.get("year") or 0), reverse=True)
    latest = by_year[0]
    y0 = int(latest.get("year") or 0)
    rev0 = latest.get("revenue")
    if rev0 is None:
        return ""
    line = f"营收({y0})：{float(rev0)/1e9:.2f}B USD"
    if len(by_year) >= 2:
        prev = by_year[1]
        y1 = int(prev.get("year") or 0)
        rev1 = prev.get("revenue")
        if rev1 and float(rev1) > 0:
            yoy = (float(rev0) - float(rev1)) / float(rev1) * 100
            line += f" · 营收同比({y0} vs {y1})：{yoy:+.1f}%"
    return line


def deep_dive_switch_page(project_root: Any) -> str:
    """返回深度分析页绝对路径，供 ``st.switch_page`` 使用。"""
    from pathlib import Path

    root = Path(project_root)
    return str(root / "frontend" / "pages" / "01_DeepDive.py")


def item_importance(item: dict[str, Any]) -> int | None:
    """读取 ``importance_score``，缺省为 None。"""
    v = item.get("importance_score")
    if v is None:
        return None
    try:
        return int(v)
    except (TypeError, ValueError):
        return None
