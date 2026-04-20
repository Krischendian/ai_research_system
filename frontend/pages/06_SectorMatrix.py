"""
跨Sector监控台 — 分析师每日工作起点
一张表扫完所有sector，决定今天重点看哪个。
"""
from __future__ import annotations

import sys
import statistics
from pathlib import Path

_fe_root = Path(__file__).resolve().parent.parent.parent
_src = _fe_root / "src"
for p in (_fe_root, _src):
    if str(p) not in sys.path:
        sys.path.insert(0, str(p))

import streamlit as st
import pandas as pd
from dotenv import load_dotenv

from research_automation.core.database import get_connection, init_db
from research_automation.extractors.fmp_client import FMPClient
from research_automation.extractors.llm_client import chat

load_dotenv(_fe_root / ".env", override=False)

st.set_page_config(page_title="Sector 监控台", layout="wide")
st.title("🖥️ Sector 监控台")
st.caption("跨sector一览，秒级定位今日重点 | 数据来源：FMP Annual Financials + Benzinga")


def _distinct_sectors() -> list[str]:
    conn = get_connection()
    try:
        init_db(conn)
        cur = conn.execute(
            "SELECT DISTINCT sector FROM companies "
            "WHERE is_active=1 AND TRIM(sector)!='' ORDER BY sector"
        )
        return [str(r[0]).strip() for r in cur.fetchall() if r[0]]
    finally:
        conn.close()


def _get_tickers(sector: str) -> list[str]:
    conn = get_connection()
    try:
        init_db(conn)
        cur = conn.execute(
            "SELECT ticker FROM companies WHERE is_active=1 AND TRIM(sector)=? ORDER BY ticker",
            (sector,),
        )
        return [r[0] for r in cur.fetchall()]
    finally:
        conn.close()


def _load_sector_financials(sector: str) -> dict:
    """拉取sector所有公司财务数据，返回sector级别统计。"""
    tickers = _get_tickers(sector)
    if not tickers:
        return {}

    fmp = FMPClient()
    yoy_list, gm_list, ebitda_list = [], [], []
    signal_count = 0

    for ticker in tickers:
        try:
            financials = fmp.get_financials(ticker, years=2)
            if not financials or len(financials) < 2:
                continue
            latest = max(financials, key=lambda r: getattr(r, "year", 0) or 0)
            prev_list = [r for r in financials if (getattr(r, "year", 0) or 0) < (getattr(latest, "year", 0) or 0)]
            prev = max(prev_list, key=lambda r: getattr(r, "year", 0) or 0) if prev_list else None

            rev = getattr(latest, "revenue", None)
            prev_rev = getattr(prev, "revenue", None) if prev else None
            if rev and prev_rev and prev_rev != 0:
                yoy = (float(rev) - float(prev_rev)) / abs(float(prev_rev)) * 100
                yoy_list.append(yoy)

            gm = getattr(latest, "gross_margin", None)
            if gm is not None:
                gm_val = float(gm) * 100 if float(gm) < 2 else float(gm)
                if 0 < gm_val < 95:
                    gm_list.append(gm_val)

            ebitda = getattr(latest, "ebitda", None)
            if ebitda is not None:
                ebitda_list.append(float(ebitda) / 1e9)

        except Exception:
            continue

    # 拉取本周新闻信号数
    try:
        from research_automation.services.signal_fetcher import fetch_signals_for_ticker
        for ticker in tickers[:5]:  # 只检查前5家，快速估算
            try:
                signals = fetch_signals_for_ticker(ticker, days_back=7)
                signal_count += len(signals)
            except Exception:
                pass
    except Exception:
        pass

    return {
        "sector": sector,
        "company_count": len(tickers),
        "median_yoy": round(statistics.median(yoy_list), 1) if yoy_list else None,
        "median_gm": round(statistics.median(gm_list), 1) if gm_list else None,
        "median_ebitda": round(statistics.median(ebitda_list), 2) if ebitda_list else None,
        "signal_count": signal_count,
    }


def _llm_sector_headline(sector: str, stats: dict) -> tuple[str, str]:
    """用LLM从缓存报告生成本季主题和关键风险（各一句话）。"""
    try:
        from research_automation.services.report_cache import get_cached_report
        from datetime import datetime, timezone
        now = datetime.now(timezone.utc)
        year = now.year
        quarter = (now.month - 1) // 3 + 1
        if quarter == 1:
            quarter, year = 4, year - 1
        else:
            quarter -= 1

        report = get_cached_report(sector, year, quarter)
        if not report:
            return "暂无缓存报告", "—"

        # 只取执行摘要部分
        lines = report.split('\n')
        exec_lines = []
        capturing = False
        for l in lines:
            if '执行摘要' in l:
                capturing = True
            if capturing and l.startswith('## ') and '执行摘要' not in l:
                break
            if capturing:
                exec_lines.append(l)

        exec_text = '\n'.join(exec_lines[:30])
        if not exec_text.strip():
            return "暂无摘要", "—"

        prompt = f"""以下是{sector}板块的执行摘要：

{exec_text}

请提取：
1. 本季核心主题：一句话（10-15字），必须有具体数字或公司名
2. 关键风险：一句话（10-15字），必须具体

严格按以下格式输出，不要其他内容：
主题：[内容]
风险：[内容]"""

        reply = chat(prompt, max_tokens=100)
        theme, risk = "—", "—"
        for line in reply.split('\n'):
            if line.startswith('主题：'):
                theme = line.replace('主题：', '').strip()
            elif line.startswith('风险：'):
                risk = line.replace('风险：', '').strip()
        return theme, risk
    except Exception:
        return "—", "—"


# ── 主界面 ────────────────────────────────────────────────────────

sectors = _distinct_sectors()
if not sectors:
    st.warning("数据库中无活跃sector。")
    st.stop()

col1, col2 = st.columns([3, 1])
with col2:
    load_btn = st.button("🔄 加载监控台", type="primary", use_container_width=True)
    use_llm = st.checkbox("生成主题/风险摘要（LLM）", value=True)

if load_btn:
    all_rows = []
    progress = st.progress(0, text="加载sector数据…")

    for i, sec in enumerate(sectors):
        progress.progress((i + 1) / len(sectors), text=f"加载 {sec}…")
        stats = _load_sector_financials(sec)
        if not stats:
            continue

        theme, risk = "—", "—"
        if use_llm:
            theme, risk = _llm_sector_headline(sec, stats)

        yoy = stats.get("median_yoy")
        gm = stats.get("median_gm")

        all_rows.append({
            "Sector": sec,
            "公司数": stats.get("company_count", 0),
            "中位Revenue YoY": f"{yoy:+.1f}%" if yoy is not None else "—",
            "中位Gross Margin": f"{gm:.1f}%" if gm is not None else "—",
            "中位EBITDA ($B)": f"${stats.get('median_ebitda', 0):.2f}B" if stats.get('median_ebitda') else "—",
            "本季主题": theme,
            "关键风险": risk,
            "本周信号数": stats.get("signal_count", 0),
            # 排序用原始值
            "_yoy_raw": yoy or 0,
            "_gm_raw": gm or 0,
        })

    progress.empty()
    st.session_state["_monitor_rows"] = all_rows

rows = st.session_state.get("_monitor_rows", [])
if rows:
    df = pd.DataFrame(rows)

    st.success(f"共 {len(df)} 个 Sector | 点击行名跳转详细报告")
    st.divider()

    # 排序选项
    sort_by = st.radio(
        "排序",
        ["默认", "Revenue YoY↓", "Gross Margin↓", "信号数↓"],
        horizontal=True,
    )
    if sort_by == "Revenue YoY↓":
        df = df.sort_values("_yoy_raw", ascending=False)
    elif sort_by == "Gross Margin↓":
        df = df.sort_values("_gm_raw", ascending=False)
    elif sort_by == "信号数↓":
        df = df.sort_values("本周信号数", ascending=False)

    display_cols = [c for c in df.columns if not c.startswith("_")]

    st.dataframe(
        df[display_cols],
        use_container_width=True,
        hide_index=True,
        height=min(80 + len(df) * 45, 600),
        column_config={
            "本季主题": st.column_config.TextColumn(width="large"),
            "关键风险": st.column_config.TextColumn(width="large"),
            "中位Revenue YoY": st.column_config.TextColumn(width="small"),
            "中位Gross Margin": st.column_config.TextColumn(width="small"),
        }
    )

    st.divider()
    st.caption("💡 点击 04_SectorReport 查看任意sector的完整六步报告")
