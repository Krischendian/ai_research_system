"""深度分析：财务数据 + 业务画像。"""
from __future__ import annotations

import sys
from pathlib import Path

# 双保险：单独打开 /DeepDive 时也能解析 `frontend` 包
_fe_root = Path(__file__).resolve().parent.parent.parent
if str(_fe_root) not in sys.path:
    sys.path.insert(0, str(_fe_root))

import pandas as pd
import requests
import streamlit as st

from frontend.streamlit_helpers import notify_api_failure

BACKEND_BASE = "http://127.0.0.1:8000"

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

ticker = st.text_input("股票代码", value="AAPL")
earnings_quarter = st.text_input("电话会季度", value="2024Q4", help="格式：2024Q4")


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
    st.session_state.earnings_error = None
    st.session_state.earnings_payload = None
    st.session_state.queried = True

    with st.spinner("加载中..."):
        sym = ticker.strip().upper()
        if not sym:
            msg = "请输入股票代码"
            st.session_state.fin_error = msg
            st.session_state.profile_error = msg
            st.session_state.earnings_error = msg
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
    tab_fin, tab_call = st.tabs(["财务与画像", "电话会议"])

    with tab_fin:
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
                "须来自披露原文的抽取结果；若为占位则说明节选/原文未明确写出。"
            )
            _sourced_block(
                prof.get("future_guidance") or "—",
                fpid.get("future_guidance") or [],
                srcp,
                unique_key=f"{prof.get('ticker')}_fg",
            )

            st.subheader("管理层对行业的判断")
            st.caption("仅收录管理层在披露中的明确表述；非模型对行业的独立判断。")
            _sourced_block(
                prof.get("industry_view") or "—",
                fpid.get("industry_view") or [],
                srcp,
                unique_key=f"{prof.get('ticker')}_iv",
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
                        st.markdown(f"> {item.get('quote') or '—'}")
                    with c_b:
                        pids = item.get("source_paragraph_ids") or []
                        if pids and st.button(
                            "📖",
                            key=f"kq_{prof.get('ticker')}_{qi}",
                            help="查看原文段落",
                        ):
                            _open_paragraph_viewer(
                                pids,
                                srcp,
                                unique_key=f"kq_d_{prof.get('ticker')}_{qi}",
                            )
            else:
                st.caption("暂无（节选内无合格逐字原话或模型未返回）。")

            st.subheader("近期动态")
            st.caption("按类型分组；每条须含「原文引用」（披露节选逐字摘录）。")
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
                            if ap and st.button(
                                "📖",
                                key=f"ca_{prof.get('ticker')}_{at_key}_{ai}",
                                help="查看原文段落",
                            ):
                                _open_paragraph_viewer(
                                    ap,
                                    srcp,
                                    unique_key=f"ca_d_{prof.get('ticker')}_{at_key}_{ai}",
                                )

            seg = prof.get("revenue_by_segment") or []
            geo = prof.get("revenue_by_geography") or []

            c1, c2 = st.columns(2)
            with c1:
                st.markdown("**业务线营收占比**")
                if seg:
                    for si, s in enumerate(seg):
                        sx1, sx2 = st.columns([10, 1])
                        with sx1:
                            st.caption(
                                f"{s.get('segment_name')} — {s.get('percentage')}"
                            )
                        with sx2:
                            gp = s.get("source_paragraph_ids") or []
                            if gp and st.button(
                                "📖",
                                key=f"seg_{prof.get('ticker')}_{si}",
                                help="查看原文段落",
                            ):
                                _open_paragraph_viewer(
                                    gp,
                                    srcp,
                                    unique_key=f"seg_d_{prof.get('ticker')}_{si}",
                                )
                else:
                    st.caption("无数据")
            with c2:
                st.markdown("**地区营收占比**")
                if geo:
                    for gi, s in enumerate(geo):
                        gx1, gx2 = st.columns([10, 1])
                        with gx1:
                            st.caption(
                                f"{s.get('segment_name')} — {s.get('percentage')}"
                            )
                        with gx2:
                            gp = s.get("source_paragraph_ids") or []
                            if gp and st.button(
                                "📖",
                                key=f"geo_{prof.get('ticker')}_{gi}",
                                help="查看原文段落",
                            ):
                                _open_paragraph_viewer(
                                    gp,
                                    srcp,
                                    unique_key=f"geo_d_{prof.get('ticker')}_{gi}",
                                )
                else:
                    st.caption("无数据")

    with tab_call:
        st.subheader("Earnings Call（财报电话会）")
        if st.session_state.earnings_error:
            st.error(f"电话会议加载失败：{st.session_state.earnings_error}")
        elif st.session_state.earnings_payload is not None:
            ec = st.session_state.earnings_payload
            st.caption(
                f"标的 `{ec.get('ticker')}` · 季度 `{ec.get('quarter')}` · "
                f"更新 `{ec.get('last_updated', '')}`"
            )
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
