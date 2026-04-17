"""深度分析：财务数据 + 业务画像。"""
from __future__ import annotations

import html
import sys
from pathlib import Path
from typing import Any, cast

# 双保险：单独打开 /DeepDive 时也能解析 `frontend` 包
_fe_root = Path(__file__).resolve().parent.parent.parent
if str(_fe_root) not in sys.path:
    sys.path.insert(0, str(_fe_root))

import pandas as pd
import plotly.express as px
import plotly.graph_objects as go
import requests
import streamlit as st

from frontend.streamlit_helpers import notify_api_failure

BACKEND_BASE = "http://127.0.0.1:8000"

# 与后端 ``normalize_equity_ticker`` 同步：常见少打字母
_TICKER_TYPOS = {"APPL": "AAPL"}

# 与 API corporate_actions.action_type 枚举一致
_CORP_ACTION_LABELS = {
    "new_business": "新业务",
    "acquisition": "收购",
    "partnership": "合作",
}


def _open_paragraph_viewer(
    paragraph_ids: list,
    source_map: dict,
    *,
    unique_key: str,
) -> None:
    """弹窗展示段落原文；无 st.dialog 时用展开区。"""
    if not paragraph_ids:
        return
    pmap = source_map or {}
    dlg = getattr(st, "dialog", None)

    def _body() -> None:
        for pid in paragraph_ids:
            st.markdown(f"**`{pid}`**")
            st.write(str(pmap.get(pid, "（无文本）")))

    if callable(dlg):

        @dlg("查看原文段落")
        def _wrapped() -> None:
            _body()

        _wrapped()
    else:
        with st.expander("📖 查看原文段落", expanded=True):
            _body()


def _open_key_quote_source_viewer(
    paragraph_ids: list,
    source_map: dict,
    *,
    source_url: str | None,
    unique_key: str,
) -> None:
    """关键原话：优先展示逐字稿段落；若无段落文本则给深度分析页链接。"""
    pmap = source_map or {}
    pids = [str(p) for p in (paragraph_ids or []) if p]
    url = (source_url or "").strip()
    dlg = getattr(st, "dialog", None)

    def _body() -> None:
        shown = False
        for pid in pids:
            blob = pmap.get(pid)
            if blob:
                shown = True
                st.markdown(f"**`{pid}`**")
                st.write(str(blob))
        if not shown and url:
            st.caption("当前响应未包含逐字稿段落全文，请在深度分析页查看。")
        if url:
            st.markdown(f"[→ 打开深度分析（电话会原文）]({url})")

    if callable(dlg):

        @dlg("原文与溯源")
        def _wrapped() -> None:
            _body()

        _wrapped()
    else:
        with st.expander("📖 原文与溯源", expanded=True):
            _body()


def _open_excerpt_dialog(title: str, excerpt: str) -> None:
    """弹窗展示模型返回的原文摘录字符串（如行业判断 industry_view_source）。"""
    body = (excerpt or "").strip()
    if not body:
        return
    dlg = getattr(st, "dialog", None)

    def _body() -> None:
        st.markdown(body)

    if callable(dlg):

        @dlg(title)
        def _wrapped() -> None:
            _body()

        _wrapped()
    else:
        with st.expander(f"📖 {title}", expanded=True):
            _body()


def _open_industry_trace_dialog(
    excerpt: str | None,
    paragraph_ids: list,
    source_map: dict,
) -> None:
    """行业判断：摘录（若有）+ 10-K 段落，统一 dialog；无 dialog 时用展开区。"""
    ex = (excerpt or "").strip()
    pids = [str(p) for p in (paragraph_ids or []) if p]
    if not ex and not pids:
        return
    pmap = source_map or {}
    dlg = getattr(st, "dialog", None)

    def _body() -> None:
        if ex:
            st.markdown("**原文依据（摘录）**")
            st.markdown(ex)
        if pids:
            if ex:
                st.divider()
            st.markdown("**10-K 原文段落**")
            for pid in pids:
                st.markdown(f"**`{pid}`**")
                st.write(str(pmap.get(pid, "（无文本）")))

    if callable(dlg):

        @dlg("行业判断 · 溯源")
        def _wrapped() -> None:
            _body()

        _wrapped()
    else:
        with st.expander("📖 行业判断 · 溯源", expanded=True):
            _body()


def _paragraph_ids_have_bodies(paragraph_ids: list, source_map: dict) -> bool:
    pmap = source_map or {}
    for p in paragraph_ids or []:
        if pmap.get(str(p)):
            return True
    return False


def _sourced_block(
    markdown_text: str,
    paragraph_ids: list,
    source_map: dict,
    *,
    unique_key: str,
) -> None:
    c1, c2 = st.columns([12, 1])
    with c1:
        st.markdown(markdown_text or "—")
    with c2:
        if paragraph_ids:
            if st.button(
                "📖",
                key=f"cite_btn_{unique_key}",
                help="查看原文段落",
            ):
                _open_paragraph_viewer(
                    paragraph_ids,
                    source_map,
                    unique_key=unique_key,
                )


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
if "earnings_error" not in st.session_state:
    st.session_state.earnings_error = None
if "earnings_payload" not in st.session_state:
    st.session_state.earnings_payload = None

# 与晨报「深度分析」跳转联动：预填 ticker 并可自动触发一次查询
if "deep_ticker_widget" not in st.session_state:
    st.session_state["deep_ticker_widget"] = "AAPL"
if "deep_earnings_quarter" not in st.session_state:
    st.session_state["deep_earnings_quarter"] = "2024Q4"
if "deep_dive_prefill_ticker" in st.session_state:
    st.session_state["deep_ticker_widget"] = st.session_state.pop(
        "deep_dive_prefill_ticker"
    )
if "deep_dive_prefill_quarter" in st.session_state:
    st.session_state["deep_earnings_quarter"] = st.session_state.pop(
        "deep_dive_prefill_quarter"
    )

# 全局搜索等外链：/DeepDive?ticker=…&quarter=… 仅在与上次 URL 签名不同时写入，避免覆盖用户已改的输入
_qp_sig = f"{st.query_params.get('ticker') or ''}|{st.query_params.get('quarter') or ''}"
if _qp_sig != st.session_state.get("_deep_dive_url_qp_sig"):
    st.session_state["_deep_dive_url_qp_sig"] = _qp_sig
    _t_qp = (st.query_params.get("ticker") or "").strip()
    _q_qp = (st.query_params.get("quarter") or "").strip()
    if _t_qp:
        st.session_state["deep_ticker_widget"] = _t_qp
    if _q_qp:
        st.session_state["deep_earnings_quarter"] = _q_qp

_auto_query = st.session_state.pop("deep_dive_auto_query", False)

ticker = st.text_input("股票代码", key="deep_ticker_widget")
earnings_quarter = st.text_input(
    "电话会季度",
    key="deep_earnings_quarter",
    help="格式：2024Q4",
)


def _fmt_usd(v: float | None) -> str:
    if v is None:
        return "—"
    return f"{v:,.0f}"


def _fmt_ratio(v: float | None) -> str:
    if v is None:
        return "—"
    return f"{v * 100:.2f}%"


def _usd_amount_to_billions(v: float | None) -> float | None:
    """
    将营收/EBITDA 等金额转为「十亿美元」用于纵轴。

    SQLite/SEC 解析常见为 **百万美元**（如 AAPL 营收约 383_285）；
    FMP 等 API 常为 **美元** 原值（≥1e9）。按量级区分，避免误用 /1e9 把百万美元压成 0。
    """
    if v is None:
        return None
    x = float(v)
    if abs(x) >= 1_000_000:
        return x / 1e9
    return x / 1e3


def _financial_trend_figure(rows: list) -> object | None:
    """最近至多三个财年：营收、EBITDA、毛利率、净负债/权益比分面折线；悬停显示数值。"""
    if not rows:
        return None
    valid = [r for r in rows if r.get("year") is not None]
    if not valid:
        return None
    sorted_rows = sorted(valid, key=lambda x: int(x["year"]))[-3:]
    metric_order = [
        "营收（十亿美元）",
        "EBITDA（十亿美元）",
        "毛利率（%）",
        "净负债/权益比",
    ]
    rec: list[dict] = []
    for r in sorted_rows:
        y = int(r["year"])
        rv = r.get("revenue")
        if rv is not None:
            try:
                b_rev = _usd_amount_to_billions(float(rv))
            except (TypeError, ValueError):
                b_rev = None
            if b_rev is not None:
                rec.append({"年份": y, "指标": "营收（十亿美元）", "数值": b_rev})
        eb = r.get("ebitda")
        if eb is not None:
            try:
                b_ebit = _usd_amount_to_billions(float(eb))
            except (TypeError, ValueError):
                b_ebit = None
            if b_ebit is not None:
                rec.append({"年份": y, "指标": "EBITDA（十亿美元）", "数值": b_ebit})
        gm = r.get("gross_margin")
        if gm is not None:
            rec.append({"年份": y, "指标": "毛利率（%）", "数值": float(gm) * 100})
        nd = r.get("net_debt_to_equity")
        if nd is not None:
            rec.append({"年份": y, "指标": "净负债/权益比", "数值": float(nd)})
    if not rec:
        return None
    df_c = pd.DataFrame(rec)
    present = [m for m in metric_order if m in set(df_c["指标"])]
    if not present:
        return None
    df_c["指标"] = pd.Categorical(df_c["指标"], categories=present, ordered=True)
    df_c = df_c.sort_values(["指标", "年份"])
    fig = px.line(
        df_c,
        x="年份",
        y="数值",
        facet_row="指标",
        markers=True,
        category_orders={"指标": present},
    )
    fig.update_layout(
        title="财务趋势（最近三年）",
        showlegend=False,
        margin=dict(l=12, r=12, t=48, b=12),
        height=max(280, 160 * len(present)),
    )
    fig.update_traces(hovertemplate="财年：%{x}<br>数值：%{y:,.4f}<extra></extra>")
    fig.update_xaxes(type="linear", dtick=1)
    fig.for_each_yaxis(lambda ax: ax.update(autorange=True, matches=None))
    return fig


def _parse_mix_percentage_str(raw: str | None) -> float | None:
    """从「45.2%」类字符串解析占比数值（用于饼图）。"""
    if raw is None or not str(raw).strip():
        return None
    t = str(raw).strip().replace("%", "").replace(",", "").strip()
    try:
        v = float(t)
    except ValueError:
        return None
    if v <= 0:
        return None
    return v


def _format_mix_absolute_hint(v: object) -> str:
    """模型或扩展字段中的绝对额（常见为美元）；无则返回空串。"""
    if v is None:
        return ""
    try:
        x = float(v)
    except (TypeError, ValueError):
        return ""
    if x <= 0:
        return ""
    if abs(x) >= 1_000_000:
        return f"金额（约）：{x / 1e9:.2f} 十亿美元"
    return f"金额（约）：{x:,.0f} 百万美元"


def _revenue_mix_pie_figure(items: list, title: str) -> go.Figure | None:
    """业务线/地区占比环形图；图例可点选隐藏扇区。"""
    names: list[str] = []
    values: list[float] = []
    custom_lines: list[str] = []
    for it in items:
        if not isinstance(it, dict):
            continue
        nm = (it.get("segment_name") or "").strip() or "—"
        pv = _parse_mix_percentage_str(it.get("percentage"))
        if pv is None:
            continue
        names.append(nm)
        values.append(pv)
        amt_raw = it.get("absolute")
        if amt_raw is None:
            amt_raw = it.get("amount_usd")
        custom_lines.append(_format_mix_absolute_hint(amt_raw))
    if not names:
        return None
    fig = go.Figure(
        data=[
            go.Pie(
                labels=names,
                values=values,
                hole=0.38,
                textinfo="percent",
                textposition="inside",
                insidetextorientation="radial",
                hovertemplate=(
                    "<b>%{label}</b><br>"
                    "占比：%{value:.2f}%"
                    "%{customdata}<extra></extra>"
                ),
                customdata=[
                    f"<br>{c}" if c else "" for c in custom_lines
                ],
            )
        ]
    )
    fig.update_layout(
        title=dict(text=title, x=0.5, xanchor="center"),
        margin=dict(t=56, b=8, l=24, r=24),
        showlegend=True,
        legend=dict(
            orientation="v",
            yanchor="middle",
            y=0.5,
            xanchor="left",
            x=1.02,
        ),
        uirevision=title,
    )
    return fig


def _mix_source_expander(
    items: list,
    srcp: dict,
    *,
    key_prefix: str,
    ticker: str,
    expander_title: str,
) -> None:
    """各分项 📖 溯源（饼图替代列表后保留）。"""
    if not items:
        return
    with st.expander(expander_title, expanded=False):
        for i, s in enumerate(items):
            if not isinstance(s, dict):
                continue
            sx1, sx2 = st.columns([10, 1])
            with sx1:
                st.caption(
                    f"{s.get('segment_name') or '—'} — {s.get('percentage') or '—'}"
                )
            with sx2:
                gp = s.get("source_paragraph_ids") or []
                if gp and st.button(
                    "📖",
                    key=f"{key_prefix}_{ticker}_{i}",
                    help="查看原文段落",
                ):
                    _open_paragraph_viewer(
                        gp,
                        srcp,
                        unique_key=f"{key_prefix}_d_{ticker}_{i}",
                    )


if st.button("查询") or _auto_query:
    st.session_state.fin_error = None
    st.session_state.fin_payload = None
    st.session_state.profile_error = None
    st.session_state.profile_payload = None
    st.session_state.earnings_error = None
    st.session_state.earnings_payload = None
    st.session_state.queried = True

    with st.spinner("加载中..."):
        # 与后端 normalize_equity_ticker 一致；勿在 text_input 实例化后写 key「deep_ticker_widget」
        sym = _TICKER_TYPOS.get(ticker.strip().upper(), ticker.strip().upper())
        if not sym:
            msg = "请输入股票代码"
            st.session_state.fin_error = msg
            st.session_state.profile_error = msg
            st.session_state.earnings_error = msg
            notify_api_failure(ValueError(msg), prefix="")
        else:
            raw_u = ticker.strip().upper()
            if raw_u != sym:
                st.caption(
                    f"已将代码 **{raw_u}** 纠正为 **{sym}** 再请求 API（与 SEC 一致）。"
                )
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

            q_raw = (earnings_quarter or "").strip() or "2024Q4"
            try:
                r3 = requests.get(
                    f"{BACKEND_BASE}/api/v1/companies/{sym}/earnings",
                    params={"quarter": q_raw},
                    timeout=180,
                )
                r3.raise_for_status()
                st.session_state.earnings_payload = r3.json()
            except Exception as e:
                st.session_state.earnings_error = notify_api_failure(
                    e, prefix="电话会议："
                )

if st.session_state.queried:
    if "deep_view_radio" not in st.session_state:
        st.session_state.deep_view_radio = "财务与画像"

    st.radio(
        "深度分析视图",
        ["财务与画像", "电话会议"],
        horizontal=True,
        key="deep_view_radio",
        label_visibility="collapsed",
    )
    _deep_tab = st.session_state.deep_view_radio

    if _deep_tab == "财务与画像":
        st.subheader("财务数据")
        if st.session_state.fin_error:
            st.error(f"财务数据加载失败：{st.session_state.fin_error}")
        elif st.session_state.fin_payload is not None:
            p = st.session_state.fin_payload
            st.caption(
                f"标的 `{p.get('ticker')}` · 数据时间 `{p.get('last_updated', '')}`"
            )
            ds = (p.get("data_source") or "").strip()
            if ds:
                st.caption(f"**财务数据来源**：{ds}")
            else:
                st.caption("**财务数据来源**：暂无（尚未从 SEC 解析入库）")
            dsl = (p.get("data_source_label") or "").strip()
            if dsl:
                st.caption(f"**数据溯源**：{dsl}")
            purl = p.get("primary_source_url")
            if purl:
                st.markdown(f"[→ SEC EDGAR 检索入口]({purl})")
            rows = p.get("financials") or []
            if not rows:
                st.info(
                    "暂无财务数据。请在项目根执行："
                    "`PYTHONPATH=src python scripts/batch_fetch_financials.py --ticker "
                    f"{p.get('ticker') or ticker} --force` 从 **SEC EDGAR** 抓取后再查询。"
                )
            else:
                trend_fig = _financial_trend_figure(rows)
                if trend_fig is not None:
                    st.plotly_chart(trend_fig, use_container_width=True)
                # 按年升序，计算 YoY%
                sorted_rows = sorted(
                    [r for r in rows if r.get("year") is not None],
                    key=lambda x: int(x["year"]),
                )

                def _yoy(curr: float | None, prev: float | None) -> str:
                    if curr is None or prev is None or prev == 0:
                        return "—"
                    pct = (curr - prev) / abs(prev) * 100
                    sign = "+" if pct >= 0 else ""
                    return f"{sign}{pct:.1f}%"

                display_rows = []
                for i, row in enumerate(sorted_rows):
                    prev = sorted_rows[i - 1] if i > 0 else None
                    nd = row.get("net_debt_to_equity")
                    prev_nd = prev.get("net_debt_to_equity") if prev else None
                    display_rows.append(
                        {
                            "财年": int(row.get("year")),
                            "营收（美元）": _fmt_usd(row.get("revenue")),
                            "营收 YoY": _yoy(
                                row.get("revenue"),
                                prev.get("revenue") if prev else None,
                            ),
                            "EBITDA（美元）": _fmt_usd(row.get("ebitda")),
                            "EBITDA YoY": _yoy(
                                row.get("ebitda"),
                                prev.get("ebitda") if prev else None,
                            ),
                            "资本支出（美元）": _fmt_usd(row.get("capex")),
                            "CAPEX YoY": _yoy(
                                row.get("capex"),
                                prev.get("capex") if prev else None,
                            ),
                            "毛利率": _fmt_ratio(row.get("gross_margin")),
                            "毛利率 YoY": _yoy(
                                row.get("gross_margin"),
                                prev.get("gross_margin") if prev else None,
                            ),
                            "净负债/权益比": (
                                f"{nd:.4f}" if nd is not None else "—"
                            ),
                            "净负债/权益比 YoY": _yoy(nd, prev_nd),
                        }
                    )
                df = pd.DataFrame(display_rows)
                st.caption(
                    "单位：美元原值（FMP）或百万美元（SEC）；YoY% 为同比变化。"
                )

                # 用颜色高亮 YoY 列：正数绿，负数红
                yoy_cols = [c for c in df.columns if "YoY" in c]

                def _color_yoy(val: str) -> str:
                    if val == "—" or not isinstance(val, str):
                        return ""
                    if val.startswith("+"):
                        return "color: #2e7d32; font-weight: bold"
                    if val.startswith("-"):
                        return "color: #c62828; font-weight: bold"
                    return ""

                _sty = cast(Any, df.style)
                try:
                    styled = _sty.map(_color_yoy, subset=yoy_cols)
                except AttributeError:
                    styled = _sty.applymap(_color_yoy, subset=yoy_cols)
                st.dataframe(styled, use_container_width=True, hide_index=True)

        st.subheader("业务画像")
        if st.session_state.profile_error:
            st.error(f"业务画像加载失败：{st.session_state.profile_error}")
        elif st.session_state.profile_payload is not None:
            prof = st.session_state.profile_payload
            st.caption(
                f"标的 `{prof.get('ticker')}` · 更新于 `{prof.get('last_updated', '')}`"
            )
            val_warn = (prof.get("validation_warning") or "").strip()
            if val_warn:
                st.warning(val_warn)
            psl = (prof.get("data_source_label") or "").strip()
            if psl:
                st.caption(f"**数据溯源**：{psl}")
            pub = prof.get("primary_source_url")
            if pub:
                st.markdown(f"[→ SEC EDGAR 检索（法定披露入口）]({pub})")
            fpid = prof.get("field_paragraph_ids") or {}
            srcp = prof.get("source_paragraphs") or {}
            _sourced_block(
                prof.get("core_business") or "—",
                fpid.get("core_business") or [],
                srcp,
                unique_key=f"{prof.get('ticker')}_cb",
            )

            st.subheader("管理层展望（未来指引）")
            st.caption(
                "须来自披露原文的抽取结果；10-K 常不提供量化指引，属披露习惯而非系统异常。"
            )
            _fg_raw = prof.get("future_guidance")
            _fg_text = (
                ""
                if _fg_raw is None
                else str(_fg_raw).strip()
            )
            _fg_placeholder = (not _fg_text) or ("未明确提及" in _fg_text)
            if _fg_placeholder:
                st.info(
                    "本次 10-K 未提供量化展望。建议查看「电话会议」部分，"
                    "管理层可能在财报电话会中透露更多细节。"
                )
                if st.button(
                    "跳转到电话会议",
                    key=f"jump_call_{prof.get('ticker')}_fg",
                ):
                    st.session_state.deep_view_radio = "电话会议"
                    st.rerun()
            else:
                _sourced_block(
                    _fg_text,
                    fpid.get("future_guidance") or [],
                    srcp,
                    unique_key=f"{prof.get('ticker')}_fg",
                )

            st.subheader("管理层对行业的判断")
            st.caption("仅收录管理层在披露中的明确表述；非模型对行业的独立判断。")
            _iv_md = str(prof.get("industry_view") or "").strip() or "—"
            _iv_src = str(prof.get("industry_view_source") or "").strip()
            _iv_pids = fpid.get("industry_view") or []
            _iv_show_trace = bool(_iv_src) or bool(_iv_pids)
            ic1, ic2 = st.columns([12, 1])
            with ic1:
                st.markdown(_iv_md)
            with ic2:
                if _iv_show_trace:
                    if st.button(
                        "📖",
                        key=f"cite_btn_{prof.get('ticker')}_iv",
                        help="查看原文摘录与 10-K 段落",
                    ):
                        _open_industry_trace_dialog(
                            _iv_src or None,
                            _iv_pids,
                            srcp,
                        )

            st.subheader("关键管理层原话（Key Quotes）")
            st.caption(
                "引号内为披露原文逐字摘录（英文保持原样）；speaker / topic 为结构化标签。"
            )
            kq = prof.get("key_quotes") or []
            if kq:
                for qi, item in enumerate(kq):
                    c_a, c_b = st.columns([12, 1])
                    with c_a:
                        st.markdown(
                            f"**{item.get('speaker') or 'UNKNOWN'}** · "
                            f"`{item.get('topic') or ''}`"
                        )
                        if item.get("data_source") == "earnings_call":
                            st.caption("来源：电话会议")
                        st.markdown(f"> {item.get('quote') or '—'}")
                    with c_b:
                        pids = item.get("source_paragraph_ids") or []
                        kq_url = str(item.get("source_url") or "").strip()
                        is_ec = item.get("data_source") == "earnings_call"
                        has_bodies = _paragraph_ids_have_bodies(pids, srcp)
                        # 电话会：逐字稿段落可在 dialog 展示；否则直接打开深度分析原文链接（新标签页）
                        if is_ec and kq_url and not has_bodies:
                            lb_kq = getattr(st, "link_button", None)
                            if callable(lb_kq):
                                lb_kq(
                                    "📖",
                                    kq_url,
                                    key=f"kq_url_{prof.get('ticker')}_{qi}",
                                    help="打开电话会原文（深度分析）",
                                )
                            else:
                                safe_u = html.escape(kq_url, quote=True)
                                st.markdown(
                                    f'<a href="{safe_u}" target="_blank" '
                                    'rel="noopener noreferrer">📖</a>',
                                    unsafe_allow_html=True,
                                )
                        elif (pids or (is_ec and kq_url)) and st.button(
                            "📖",
                            key=f"kq_{prof.get('ticker')}_{qi}",
                            help="查看原文段落或电话会溯源",
                        ):
                            if is_ec:
                                _open_key_quote_source_viewer(
                                    pids,
                                    srcp,
                                    source_url=kq_url or None,
                                    unique_key=f"kq_ec_{prof.get('ticker')}_{qi}",
                                )
                            else:
                                _open_paragraph_viewer(
                                    pids,
                                    srcp,
                                    unique_key=f"kq_d_{prof.get('ticker')}_{qi}",
                                )
            else:
                st.caption("暂无（节选内无合格逐字原话或模型未返回）。")

            st.subheader("近期动态")
            st.caption(
                "按类型分组。10-K 条目点 📖 打开段落弹窗；新闻条目点 📖 在新标签页打开原文链接。"
            )
            actions = prof.get("corporate_actions") or []
            by_type: dict[str, list] = {}
            for a in actions:
                t = (a.get("action_type") or "").strip()
                if not t:
                    continue
                by_type.setdefault(t, []).append(a)
            if not actions:
                st.caption("暂无近期结构化动态。")
            else:
                for at_key in ("new_business", "acquisition", "partnership"):
                    items = by_type.get(at_key, [])
                    if not items:
                        continue
                    label = _CORP_ACTION_LABELS.get(at_key, at_key)
                    st.markdown(f"**{label}**")
                    for ai, item in enumerate(items):
                        cx1, cx2 = st.columns([12, 1])
                        with cx1:
                            st.markdown(f"- {item.get('description') or '—'}")
                            d = item.get("date")
                            if d:
                                st.caption(f"日期：{d}")
                            sq = item.get("source_quote")
                            if sq:
                                st.caption(f"原文引用：{sq}")
                        with cx2:
                            ap = item.get("source_paragraph_ids") or []
                            ca_url = str(item.get("source_url") or "").strip()
                            if ca_url:
                                lb_ca = getattr(st, "link_button", None)
                                if callable(lb_ca):
                                    lb_ca(
                                        "📖",
                                        ca_url,
                                        key=f"ca_url_{prof.get('ticker')}_{at_key}_{ai}",
                                        help="在新标签页打开新闻原文",
                                    )
                                else:
                                    safe = html.escape(ca_url, quote=True)
                                    st.markdown(
                                        f'<a href="{safe}" target="_blank" '
                                        'rel="noopener noreferrer">📖</a>',
                                        unsafe_allow_html=True,
                                    )
                            if ap and st.button(
                                "📖",
                                key=f"ca_p_{prof.get('ticker')}_{at_key}_{ai}",
                                help="查看 10-K 原文段落",
                            ):
                                _open_paragraph_viewer(
                                    ap,
                                    srcp,
                                    unique_key=f"ca_d_{prof.get('ticker')}_{at_key}_{ai}",
                                )

            seg = prof.get("revenue_by_segment") or []
            geo = prof.get("revenue_by_geography") or []
            _t = str(prof.get("ticker") or ticker or "x")

            c1, c2 = st.columns(2)
            with c1:
                fig_seg = _revenue_mix_pie_figure(seg, "业务线营收占比")
                if fig_seg is None:
                    st.markdown(
                        '<p style="color:#888;margin:0.2rem 0;">无数据</p>',
                        unsafe_allow_html=True,
                    )
                else:
                    st.plotly_chart(fig_seg, use_container_width=True)
                    st.caption("点击图例可隐藏/显示对应扇区；悬停查看占比与金额（若有）。")
                _mix_source_expander(
                    seg,
                    srcp,
                    key_prefix="seg",
                    ticker=_t,
                    expander_title="业务线 · 原文段落",
                )
            with c2:
                fig_geo = _revenue_mix_pie_figure(geo, "地区营收占比")
                if fig_geo is None:
                    st.markdown(
                        '<p style="color:#888;margin:0.2rem 0;">无数据</p>',
                        unsafe_allow_html=True,
                    )
                else:
                    st.plotly_chart(fig_geo, use_container_width=True)
                    st.caption("点击图例可隐藏/显示对应扇区；悬停查看占比与金额（若有）。")
                _mix_source_expander(
                    geo,
                    srcp,
                    key_prefix="geo",
                    ticker=_t,
                    expander_title="地区 · 原文段落",
                )

    else:
        st.subheader("Earnings Call（财报电话会）")
        if st.session_state.earnings_error:
            st.error(f"电话会议加载失败：{st.session_state.earnings_error}")
        elif st.session_state.earnings_payload is not None:
            ec = st.session_state.earnings_payload
            st.caption(
                f"标的 `{ec.get('ticker')}` · 季度 `{ec.get('quarter')}` · "
                f"更新 `{ec.get('last_updated', '')}`"
            )
            ec_ds = (ec.get("data_source") or "").strip()
            ds_label = {
                "fmp": "FMP",
                "sec_8k": "SEC 8-K (EDGAR)",
                "sec_api": "SEC (sec-api.io)",
                "earningscall": "earningscall",
            }.get(ec_ds, ec_ds)
            if ec_ds:
                st.caption(f"**电话会逐字稿来源**：{ds_label}")
            else:
                st.caption("**电话会逐字稿来源**：无（本响应不应出现，若出现请反馈）")
            dsl = (ec.get("data_source_label") or "").strip()
            if dsl:
                st.caption(f"**数据溯源**：{dsl}")
            ec_src = ec.get("source_paragraphs") or {}
            st.markdown("**摘要**")
            s1, s2 = st.columns([12, 1])
            with s1:
                st.markdown(ec.get("summary") or "—")
            with s2:
                sid = ec.get("summary_source_paragraph_ids") or []
                if sid and st.button(
                    "📖",
                    key=f"ec_sum_{ec.get('ticker')}_{ec.get('quarter')}",
                    help="查看原文段落",
                ):
                    _open_paragraph_viewer(
                        sid,
                        ec_src,
                        unique_key=f"ec_sum_d_{ec.get('ticker')}",
                    )
            st.markdown("**管理层观点**")
            for i, pt in enumerate(ec.get("management_viewpoints") or [], start=1):
                txt = pt.get("text") if isinstance(pt, dict) else str(pt)
                pvid = (
                    (pt.get("source_paragraph_ids") or []) if isinstance(pt, dict) else []
                )
                vx1, vx2 = st.columns([12, 1])
                with vx1:
                    st.markdown(f"{i}. {txt}")
                with vx2:
                    if pvid and st.button(
                        "📖",
                        key=f"ec_mv_{ec.get('ticker')}_{i}",
                        help="查看原文段落",
                    ):
                        _open_paragraph_viewer(
                            pvid,
                            ec_src,
                            unique_key=f"ec_mv_d_{ec.get('ticker')}_{i}",
                        )
            if not (ec.get("management_viewpoints") or []):
                st.caption("无")
            st.markdown("**重要原话**")
            qrows = ec.get("quotations") or []
            if qrows:
                for qi, x in enumerate(qrows):
                    qx1, qx2 = st.columns([12, 1])
                    with qx1:
                        st.markdown(
                            f"**{x.get('speaker') or ''}** · `{x.get('topic') or ''}`"
                        )
                        st.markdown(f"> {x.get('quote') or '—'}")
                    with qx2:
                        qp = x.get("source_paragraph_ids") or []
                        if qp and st.button(
                            "📖",
                            key=f"ec_q_{ec.get('ticker')}_{qi}",
                            help="查看原文段落",
                        ):
                            _open_paragraph_viewer(
                                qp,
                                ec_src,
                                unique_key=f"ec_q_d_{ec.get('ticker')}_{qi}",
                            )
            else:
                st.caption("无")
            st.markdown("**新业务 / 战略要点**")
            for i, h in enumerate(ec.get("new_business_highlights") or [], start=1):
                ht = h.get("text") if isinstance(h, dict) else str(h)
                hp = (
                    (h.get("source_paragraph_ids") or []) if isinstance(h, dict) else []
                )
                hx1, hx2 = st.columns([12, 1])
                with hx1:
                    st.markdown(f"{i}. {ht}")
                with hx2:
                    if hp and st.button(
                        "📖",
                        key=f"ec_nb_{ec.get('ticker')}_{i}",
                        help="查看原文段落",
                    ):
                        _open_paragraph_viewer(
                            hp,
                            ec_src,
                            unique_key=f"ec_nb_d_{ec.get('ticker')}_{i}",
                        )
            if not (ec.get("new_business_highlights") or []):
                st.caption("无")
