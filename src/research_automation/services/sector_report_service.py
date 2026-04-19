"""按 ``sector`` 汇总 Tavily 新闻信号与 FMP 内部交易，生成 Markdown 行业报告。"""
from __future__ import annotations

import logging
import os
import re
from datetime import date, datetime, timedelta, timezone
from typing import Any

from research_automation.core.company_manager import CompanyRecord, list_companies
from research_automation.extractors.fmp_client import get_insider_trades
from research_automation.services.insider_service import get_insider_summary
from research_automation.services.signal_fetcher import (
    SignalFetchStats,
    company_display_name,
    fetch_signals_for_ticker,
)

logger = logging.getLogger(__name__)


def _relevance_threshold_from_env() -> int:
    """报告用相关性下限，默认 1（可通过 ``REPORT_RELEVANCE_THRESHOLD`` 覆盖）。"""
    raw = (os.getenv("REPORT_RELEVANCE_THRESHOLD") or "1").strip()
    try:
        return max(0, min(3, int(raw)))
    except ValueError:
        return 1


def _fmt_usd(v: float | None) -> str:
    if v is None or (isinstance(v, float) and v != v):
        return "—"
    x = float(v)
    ax = abs(x)
    if ax >= 1e9:
        return f"${x/1e9:.2f}B"
    if ax >= 1e6:
        return f"${x/1e6:.2f}M"
    if ax >= 1e3:
        return f"${x/1e3:.2f}K"
    return f"${x:,.0f}"


def _fmt_trade_side_value(value: float | None, trade_count: int) -> str:
    """有笔数但金额不可得时写「股数未披露」，与 ``insider_service`` 语义一致。"""
    if trade_count <= 0:
        return "—"
    if value is None:
        return "股数未披露"
    return _fmt_usd(value)


def _md_escape_title(s: str, max_len: int = 200) -> str:
    """避免标题内换行/反引号破坏 Markdown 列表。"""
    t = (s or "").replace("\r\n", "\n").replace("\r", "\n")
    t = re.sub(r"\s+", " ", t).strip()
    if len(t) > max_len:
        t = t[: max_len - 1] + "…"
    return t.replace("`", "'")


def _excerpt(content: str, max_len: int = 220) -> str:
    t = (content or "").replace("\r\n", "\n").replace("\r", "\n")
    t = re.sub(r"\s+", " ", t).strip()
    if len(t) > max_len:
        return t[: max_len - 1] + "…"
    return t


def _filter_sort_signals(
    signals: list[dict[str, Any]], threshold: int
) -> tuple[list[dict[str, Any]], int]:
    """
    按 ``relevance_score`` 降序，仅保留 ``>= threshold``。
    返回 ``(过滤后列表, 被阈值挡掉的条数)``。
    """
    below = 0
    out: list[dict[str, Any]] = []
    for s in signals:
        rs = int(s.get("relevance_score") or 0)
        if rs < threshold:
            below += 1
            continue
        out.append(s)
    out.sort(
        key=lambda r: (
            -int(r.get("relevance_score") or 0),
            str(r.get("published_date") or ""),
        )
    )
    return out, below


def generate_sector_report(
    sector: str,
    days_back: int = 7,
    *,
    relevance_threshold: int | None = None,
    report_stats: dict[str, Any] | None = None,
) -> str:
    """
    查询 ``companies`` 中该 ``sector`` 的活跃标的，逐家拉取 Tavily 信号与 FMP 内部交易摘要，
    返回 Markdown 字符串（含行业概览与各公司详情）。

    新闻侧：``fetch_signals_for_ticker`` 已完成 UTC 日期窗口、URL 去重与噪音过滤；
    本函数再按 ``relevance_score`` 过滤、全报告跨标的 URL 去重，避免重复链接。

    ``report_stats`` 若传入则写入原始条数、过滤后条数、去重/过期等调试字段。
    """
    sec = (sector or "").strip()
    thr = (
        int(relevance_threshold)
        if relevance_threshold is not None
        else _relevance_threshold_from_env()
    )
    thr = max(0, min(3, thr))

    fetch_tot = SignalFetchStats()
    stats: dict[str, Any] = {
        "relevance_threshold": thr,
        "raw_signal_count": 0,
        "filtered_signal_count": 0,
        "below_relevance_dropped": 0,
        "cross_ticker_duplicate_urls": 0,
        "fetch_aggregate": fetch_tot,
    }
    if report_stats is not None:
        report_stats.clear()

    if not sec:
        return "# 行业监控报告\n\n（sector 为空）\n"

    companies = list_companies(sector=sec, active_only=True)
    if not companies:
        return (
            f"# 行业监控报告：{sec}\n\n"
            f"**生成时间**：{datetime.now(timezone.utc).strftime('%Y-%m-%d %H:%M')} UTC\n\n"
            "未找到该 sector 的活跃公司（请检查 `companies` 表）。\n"
        )

    per_company: list[
        tuple[CompanyRecord, list[dict[str, Any]], dict[str, Any], int, bool]
    ] = []
    # 全报告级 URL：后出现的标的若与同篇 URL 重复则跳过，降低「一篇多公司」重复展示
    seen_urls_global: set[str] = set()
    raw_total = 0
    filtered_total = 0
    below_thr_total = 0
    dup_cross = 0

    for rec in companies:
        t = rec.ticker
        sym_stats = SignalFetchStats()
        try:
            signals = fetch_signals_for_ticker(
                t,
                days_back=days_back,
                company_name=(rec.company_name or "").strip() or None,
                stats_out=sym_stats,
            )
        except Exception:
            logger.exception("Tavily 信号拉取失败 ticker=%s", t)
            signals = []
            sym_stats = SignalFetchStats()

        fetch_tot.merge_from(sym_stats)

        had_fetch_signals = len(signals) > 0
        raw_total += len(signals)
        filtered, below_n = _filter_sort_signals(signals, thr)
        below_thr_total += below_n

        deduped_for_company: list[dict[str, Any]] = []
        for s in filtered:
            u = (str(s.get("url") or "")).strip().lower()
            if u:
                if u in seen_urls_global:
                    dup_cross += 1
                    continue
                seen_urls_global.add(u)
            deduped_for_company.append(s)

        filtered_total += len(deduped_for_company)

        try:
            insider = get_insider_summary(t, days_back=days_back)
        except Exception:
            logger.exception("内部交易汇总失败 ticker=%s", t)
            insider = get_insider_summary("", days_back=days_back)

        per_company.append((rec, deduped_for_company, insider, below_n, had_fetch_signals))

    stats["raw_signal_count"] = raw_total
    stats["filtered_signal_count"] = filtered_total
    stats["below_relevance_dropped"] = below_thr_total
    stats["cross_ticker_duplicate_urls"] = dup_cross
    if report_stats is not None:
        report_stats.update(stats)

    all_signals: list[dict[str, Any]] = []
    layoff_n = business_n = insider_news_n = other_n = 0
    pool_buy_val = 0.0
    pool_sell_val = 0.0
    pool_buy_has = pool_sell_has = False
    pool_buy_trades = pool_sell_trades = 0

    for rec, signals, insider, _below, _had in per_company:
        for s in signals:
            all_signals.append(s)
            st = str(s.get("signal_type") or "other")
            if st == "layoff":
                layoff_n += 1
            elif st == "business_change":
                business_n += 1
            elif st == "insider_trade":
                insider_news_n += 1
            else:
                other_n += 1

        tb = insider.get("total_buy_value")
        ts = insider.get("total_sell_value")
        if isinstance(tb, (int, float)) and tb == tb:
            pool_buy_val += float(tb)
            pool_buy_has = True
        if isinstance(ts, (int, float)) and ts == ts:
            pool_sell_val += float(ts)
            pool_sell_has = True
        pool_buy_trades += int(insider.get("buy_count") or 0)
        pool_sell_trades += int(insider.get("sell_count") or 0)

    total_signals = len(all_signals)
    now = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M")
    insider_line = (
        f"FMP 池内合计：买入 **{pool_buy_trades}** 笔"
        f"（总价值 {_fmt_trade_side_value(pool_buy_val if pool_buy_has else None, pool_buy_trades)}）"
        f"，卖出 **{pool_sell_trades}** 笔"
        f"（总价值 {_fmt_trade_side_value(pool_sell_val if pool_sell_has else None, pool_sell_trades)}）"
    )

    lines: list[str] = [
        f"# 行业监控报告：{sec}",
        f"**生成时间**：{now} UTC",
        f"**监控周期**：过去 {int(days_back)} 天",
        f"**新闻相关性阈值**：`relevance_score` ≥ **{thr}**",
        "",
        "## 行业概览",
        f"- 总信号数（新闻，已按阈值与全池 URL 去重）：**{total_signals}**",
        f"- 裁员相关（`layoff`）：**{layoff_n}**",
        f"- 业务变化（`business_change`）：**{business_n}**",
        f"- 新闻中含内部交易语境（`insider_trade`）：**{insider_news_n}**",
        f"- 其他（`other`）：**{other_n}**",
        f"- 内部交易（FMP 申报）：{insider_line}",
        "",
        "## 公司详情",
        "",
    ]

    for rec, signals, insider, _below, had_fetch_signals in per_company:
        t = rec.ticker
        disp = company_display_name(t, rec.company_name)
        lines.append(f"### {t} — {disp}")
        lines.append("")
        lines.append("#### 📰 新闻信号")
        lines.append("")
        if not signals:
            lines.append("*暂无高相关信号*" if had_fetch_signals else "*暂无*")
            lines.append("")
        else:
            for s in signals:
                st = str(s.get("signal_type") or "other")
                rs = int(s.get("relevance_score") or 0)
                title = _md_escape_title(str(s.get("title") or "(no title)"))
                url = str(s.get("url") or "").strip()
                ex = _excerpt(str(s.get("content") or ""))
                lines.append(f"- **[{st}]** · 相关性 **{rs}** — {title}")
                if url:
                    lines.append(f"  - 📎 [原文链接]({url})")
                if ex:
                    lines.append(f"  - > {ex}")
                lines.append("")

        lines.append("#### 💼 内部交易（FMP）")
        lines.append("")
        bc = int(insider.get("buy_count") or 0)
        sc = int(insider.get("sell_count") or 0)
        tbv = insider.get("total_buy_value")
        tsv = insider.get("total_sell_value")
        if insider.get("trade_count", 0) == 0:
            lines.append("*暂无（窗口内无申报或无法解析日期）*")
        else:
            lines.append(
                f"- 买入：**{bc}** 笔，总价值 **{_fmt_trade_side_value(float(tbv) if isinstance(tbv, (int, float)) and tbv == tbv else None, bc)}**"
            )
            lines.append(
                f"- 卖出：**{sc}** 笔，总价值 **{_fmt_trade_side_value(float(tsv) if isinstance(tsv, (int, float)) and tsv == tsv else None, sc)}**"
            )
            tops = insider.get("top_insiders") or []
            if tops:
                parts = []
                for it in tops[:5]:
                    nm = str(it.get("insiderName") or "").strip()
                    tc = int(it.get("trades") or 0)
                    tv = it.get("total_value")
                    fv = float(tv) if isinstance(tv, (int, float)) and tv == tv else None
                    parts.append(
                        f"{nm}（{tc} 笔，约 {_fmt_trade_side_value(fv, tc)}）"
                    )
                lines.append(f"- 主要内部人士：{'; '.join(parts)}")
            lines.append(
                f"- 窗口内申报条数：**{int(insider.get('trade_count') or 0)}**"
            )
        lines.append("")

    # 调试统计（便于验收「噪音比例 / 去重 / 过期」）
    fa: SignalFetchStats = stats["fetch_aggregate"]
    noise_ratio = (
        (fa.dropped_noise + fa.dropped_expired + fa.dropped_other + below_thr_total)
        / max(1, fa.raw_row_count)
    )
    lines.extend(
        [
            "---",
            "## 调试统计",
            f"- Tavily 原始返回行数（全池合计）：**{fa.raw_row_count}**",
            f"- 合并 URL 后唯一条数（全池合计）：**{fa.unique_url_count}**",
            f"- 近似去重行数（跨查询重复 URL）：**{fa.dropped_duplicate_rows}**",
            f"- 因发布日期超出窗口丢弃（UTC）：**{fa.dropped_expired}**",
            f"- 无意义/噪音丢弃：**{fa.dropped_noise}**",
            f"- 其他规则丢弃（分析师噪声、泛化页、标的弱相关等）：**{fa.dropped_other}**",
            f"- 过 fetch 后条数合计：**{raw_total}**",
            f"- 相关性 < {thr} 丢弃：**{below_thr_total}**",
            f"- 全报告跨标的重复 URL 跳过：**{dup_cross}**",
            f"- 写入报告的新闻条数：**{filtered_total}**",
            f"- 粗算噪音/剔除占比（相对 Tavily 原始行）：**{noise_ratio:.1%}**",
            "",
        ]
    )

    return "\n".join(lines).rstrip() + "\n"


# ---------------------------------------------------------------------------
# 六步结构行业报告（新）
# ---------------------------------------------------------------------------


def _resolve_sector_news_days_back(days_back: int | None) -> int:
    if days_back is not None:
        return max(1, min(30, int(days_back)))
    raw = (os.getenv("SECTOR_REPORT_NEWS_DAYS_BACK") or "7").strip()
    try:
        return max(1, min(30, int(raw)))
    except ValueError:
        return 7


def _load_per_company_signals_and_insiders(
    sector: str,
    days_back: int,
    thr: int,
    report_stats: dict[str, Any] | None,
) -> (
    tuple[
        list[CompanyRecord],
        list[
            tuple[
                CompanyRecord,
                list[dict[str, Any]],
                dict[str, Any],
                int,
                bool,
            ]
        ],
        dict[str, Any],
        SignalFetchStats,
    ]
    | None
):
    sec = (sector or "").strip()
    thr = max(0, min(3, thr))
    fetch_tot = SignalFetchStats()
    stats: dict[str, Any] = {
        "relevance_threshold": thr,
        "raw_signal_count": 0,
        "filtered_signal_count": 0,
        "below_relevance_dropped": 0,
        "cross_ticker_duplicate_urls": 0,
        "fetch_aggregate": fetch_tot,
    }
    if report_stats is not None:
        report_stats.clear()
    if not sec:
        return None
    companies = list_companies(sector=sec, active_only=True)
    if not companies:
        if report_stats is not None:
            report_stats.update(stats)
        return None

    per_company: list[
        tuple[
            CompanyRecord,
            list[dict[str, Any]],
            dict[str, Any],
            int,
            bool,
        ]
    ] = []
    seen_urls_global: set[str] = set()
    raw_total = filtered_total = below_thr_total = dup_cross = 0

    for rec in companies:
        t = rec.ticker
        sym_stats = SignalFetchStats()
        try:
            signals = fetch_signals_for_ticker(
                t,
                days_back=days_back,
                company_name=(rec.company_name or "").strip() or None,
                stats_out=sym_stats,
            )
        except Exception:
            logger.exception("信号拉取失败 ticker=%s", t)
            signals = []
            sym_stats = SignalFetchStats()

        fetch_tot.merge_from(sym_stats)
        had_fetch_signals = len(signals) > 0
        raw_total += len(signals)

        filtered_signals = [
            s for s in signals if int(s.get("relevance_score") or 0) >= thr
        ]
        below_n = len(signals) - len(filtered_signals)
        below_thr_total += below_n

        deduped: list[dict[str, Any]] = []
        for s in filtered_signals:
            u = str(s.get("url") or "").strip().lower()
            if u:
                if u in seen_urls_global:
                    dup_cross += 1
                    continue
                seen_urls_global.add(u)
            deduped.append(s)
        filtered_total += len(deduped)

        try:
            rows_all = get_insider_trades(t, limit=max(50, days_back * 3))
            cutoff = date.today() - timedelta(days=max(1, int(days_back)))
            filtered_trades: list[dict[str, Any]] = []
            for r in rows_all:
                if not isinstance(r, dict):
                    continue
                raw_d = str(r.get("transactionDate") or "").strip()[:10]
                if len(raw_d) < 10:
                    raw_d = str(r.get("filingDate") or "").strip()[:10]
                if len(raw_d) < 10:
                    continue
                try:
                    td = datetime.strptime(raw_d, "%Y-%m-%d").date()
                except ValueError:
                    continue
                if td < cutoff:
                    continue
                filtered_trades.append(r)
            insider = _summarize_insider_trades(filtered_trades)
        except Exception:
            logger.exception("内部交易汇总失败 ticker=%s", t)
            insider = {}

        per_company.append((rec, deduped, insider, below_n, had_fetch_signals))

    stats["raw_signal_count"] = raw_total
    stats["filtered_signal_count"] = filtered_total
    stats["below_relevance_dropped"] = below_thr_total
    stats["cross_ticker_duplicate_urls"] = dup_cross
    if report_stats is not None:
        report_stats.update(stats)
    return companies, per_company, stats, fetch_tot


def _fmt_pct(v: float | None) -> str:
    if v is None:
        return "—"
    return f"{v * 100:.1f}%"


def _fmt_roc(v: float | None) -> str:
    if v is None:
        return "—"
    sign = "+" if v >= 0 else ""
    return f"{sign}{v * 100:.1f}%"


def _confidence_zh(c: str) -> str:
    return {"high": "高", "medium": "中", "low": "低"}.get(
        str(c or "").strip().lower(), "低"
    )


def _step1_sector_business_overview(
    per_company: list[
        tuple[
            CompanyRecord,
            list[dict[str, Any]],
            dict[str, Any],
            int,
            bool,
        ]
    ],
) -> list[str]:
    from research_automation.extractors.fmp_client import get_segment_revenue
    lines: list[str] = ["## Step 1｜Sector 业务全景", ""]
    lines.append("各公司最新财年业务线收入拆分（FMP revenue-product-segmentation）：")
    lines.append("")
    found_any = False
    for rec, _signals, _insider, _below, _had in per_company:
        t = rec.ticker
        disp = company_display_name(t, rec.company_name)
        data = get_segment_revenue(t, 2024)
        if data is None:
            data = get_segment_revenue(t, 2023)
        if not data:
            continue
        found_any = True
        total = sum(d["absolute"] for d in data)
        total_b = total / 1e9
        lines.append(f"**{disp} ({t})**　总收入 ${total_b:.1f}B")
        for seg in data:
            bar = "█" * int(seg["percentage"] / 5)
            lines.append(f"- {seg['segment']}: {seg['percentage']:.1f}%　{bar}　(${seg['absolute']/1e9:.2f}B)")
        lines.append("")
    if not found_any:
        lines.append("*暂无可用的revenue breakdown数据（FMP未收录或非美股）*")
        lines.append("")
    return lines


def _step2_per_company_revenue_breakdown(
    per_company: list[
        tuple[
            CompanyRecord,
            list[dict[str, Any]],
            dict[str, Any],
            int,
            bool,
        ]
    ],
) -> list[str]:
    from research_automation.extractors.fmp_client import get_segment_revenue
    lines: list[str] = ["## Step 2｜每家公司业务占比", ""]
    for rec, _signals, _insider, _below, _had in per_company:
        t = rec.ticker
        disp = company_display_name(t, rec.company_name)
        lines.append(f"### {t} — {disp}")
        lines.append("")
        data = get_segment_revenue(t, 2024)
        year_used = 2024
        if data is None:
            data = get_segment_revenue(t, 2023)
            year_used = 2023
        if not data:
            lines.append("*暂无revenue breakdown数据（FMP未收录或非美股）*")
            lines.append("")
            continue
        total = sum(d["absolute"] for d in data)
        lines.append(f"**财年{year_used} revenue breakdown**（总计 ${total/1e9:.2f}B）：")
        lines.append("")
        lines.append("| 业务线 | 占比 | 收入 |")
        lines.append("|--------|------|------|")
        for seg in data:
            lines.append(f"| {seg['segment']} | {seg['percentage']:.1f}% | ${seg['absolute']/1e9:.2f}B |")
        lines.append("")
    return lines


def _step3_per_company_outlook(
    per_company: list[
        tuple[
            CompanyRecord,
            list[dict[str, Any]],
            dict[str, Any],
            int,
            bool,
        ]
    ],
) -> list[str]:
    from research_automation.services.profile_service import (
        ProfileGenerationError,
        get_profile,
    )

    lines: list[str] = ["## Step 3｜展望与战略重心", ""]
    for rec, _signals, _insider, _below, _had in per_company:
        t = rec.ticker
        disp = company_display_name(t, rec.company_name)
        lines.append(f"### {t} — {disp}")
        lines.append("")
        try:
            profile = get_profile(t)
            fg = (profile.future_guidance or "").strip()
            if fg and fg not in ("原文未明确提及", "NOT_FOUND"):
                lines.append("**未来展望与指引：**")
                lines.append("")
                lines.append(fg)
                lines.append("")
            else:
                lines.append("**未来展望与指引：** *原文未明确提及*")
                lines.append("")
            iv = (profile.industry_view or "").strip()
            if iv and iv not in ("原文未明确提及", "NOT_FOUND"):
                lines.append("**行业判断（管理层视角）：**")
                lines.append("")
                lines.append(iv)
                if profile.industry_view_source:
                    lines.append(f"*原文依据：{profile.industry_view_source}*")
                lines.append("")
            else:
                lines.append("**行业判断（管理层视角）：** *原文未明确提及*")
                lines.append("")
            fwd_quotes = [
                q
                for q in (profile.key_quotes or [])
                if getattr(q, "modality", "") == "forward_looking"
            ]
            if fwd_quotes:
                lines.append("**前瞻性原话：**")
                lines.append("")
                for q in fwd_quotes[:3]:
                    lines.append(f"> **{q.speaker or 'UNKNOWN'}**：\"{q.quote}\"")
                    lines.append(f"> *主题：{q.topic}*")
                    lines.append("")
        except ProfileGenerationError as e:
            lines.append(f"*（画像生成失败：{e.message}）*")
            lines.append("")
        except Exception:
            logger.exception("Step3 get_profile 失败 ticker=%s", t)
            lines.append("*（画像拉取失败，详见日志）*")
            lines.append("")
    return lines


def _step4_earning_call_section(
    _sector: str,
    _earnings_cross_review: dict[str, Any] | None,
    _quarters: list[str] | None,
    sector_watch_items: list[str] | None = None,
    per_company: list[
        tuple[
            CompanyRecord,
            list[dict[str, Any]],
            dict[str, Any],
            int,
            bool,
        ]
    ]
    | None = None,
    current_year: int | None = None,
    current_quarter: int | None = None,
) -> list[str]:
    from research_automation.services.earnings_service import (
        EarningsAnalysisError,
        analyze_earnings_call,
    )

    lines: list[str] = ["## Step 4｜Earning Call 内容", ""]
    if sector_watch_items:
        lines.append(f"**本sector关注项**：{', '.join(sector_watch_items)}")
        lines.append("")
    now = datetime.now(timezone.utc)
    year = current_year or now.year
    quarter = current_quarter or ((now.month - 1) // 3 + 1)
    # 当前季度财报尚未发布，自动退回上一季度
    if current_quarter is None:
        if quarter == 1:
            quarter = 4
            year -= 1
        else:
            quarter -= 1

    if per_company:
        has_any = False
        for rec, _signals, _insider, _below, _had in per_company:
            t = rec.ticker
            disp = company_display_name(t, rec.company_name)
            lines.append(f"### {t} — {disp}")
            lines.append("")
            try:
                analysis = analyze_earnings_call(
                    t,
                    year,
                    quarter,
                    sector_watch_items=sector_watch_items,
                )
                has_any = True
                lines.append("**概括：**")
                lines.append("")
                lines.append(analysis.summary or "*（无概括）*")
                lines.append("")
                if analysis.management_viewpoints:
                    lines.append("**管理层核心观点：**")
                    lines.append("")
                    for vp in analysis.management_viewpoints:
                        lines.append(f"- {vp.text}")
                    lines.append("")
                if analysis.quotations:
                    lines.append("**关键原话：**")
                    lines.append("")
                    for q in analysis.quotations:
                        lines.append(
                            f"> **{q.speaker or 'Unknown'}**：\"{q.quote}\""
                        )
                        lines.append(f"> *主题：{q.topic}*")
                        lines.append("")
                if analysis.new_business_highlights:
                    lines.append("**新业务 / 战略动向：**")
                    lines.append("")
                    for nb in analysis.new_business_highlights:
                        lines.append(f"- {nb.text}")
                    lines.append("")
            except EarningsAnalysisError as e:
                lines.append(f"*（逐字稿不可用：{e.message}）*")
                lines.append("")
            except Exception:
                logger.exception("Step4 earnings 失败 ticker=%s", t)
                lines.append("*（分析失败，详见日志）*")
                lines.append("")
        if not has_any:
            lines.append("*（本sector所有公司均无可用逐字稿）*")
            lines.append("")
        return lines
    lines.append("*（未传入 earnings_cross_review，或无电话会数据）*")
    lines.append("")
    return lines


def _summarize_insider_trades(trades: list[Any]) -> dict[str, Any]:
    """把 get_insider_trades 返回的 list 汇总为 _step5 需要的 dict 格式。"""
    if not trades:
        return {}

    def _ttu(tr: Any) -> str:
        return str(tr.get("transactionType") or "").strip().upper()

    buys = [
        t
        for t in trades
        if _ttu(t) in ("P", "BUY", "PURCHASE")
    ]
    sells = [
        t
        for t in trades
        if _ttu(t) in ("S", "SELL", "SALE")
    ]

    def _row_money(tr: dict[str, Any]) -> float:
        for k in ("value", "totalValue"):
            v = tr.get(k)
            if v is None:
                continue
            try:
                return float(v)
            except (TypeError, ValueError):
                continue
        return 0.0

    buy_val = sum(_row_money(t) for t in buys)
    sell_val = sum(_row_money(t) for t in sells)
    bc, sc = len(buys), len(sells)
    return {
        "buy_count": bc,
        "sell_count": sc,
        "buy_value": buy_val,
        "sell_value": sell_val,
        "total_buy_value": buy_val if bc else None,
        "total_sell_value": sell_val if sc else None,
        "trade_count": len(trades),
    }


def _step5_new_biz_acquisitions_insider(
    per_company: list[
        tuple[
            CompanyRecord,
            list[dict[str, Any]],
            dict[str, Any],
            int,
            bool,
        ]
    ],
) -> list[str]:
    lines: list[str] = ["## Step 5｜新业务 / 收购 / Insider 异动", ""]
    for rec, signals, insider, _below, _had in per_company:
        t = rec.ticker
        disp = company_display_name(t, rec.company_name)
        biz_signals = [
            s
            for s in signals
            if str(s.get("signal_type") or "")
            in ("business_change", "insider_trade")
        ]
        if not biz_signals and int(insider.get("trade_count") or 0) == 0:
            continue
        lines.append(f"### {t} — {disp}")
        lines.append("")
        if biz_signals:
            lines.append("**新业务 / 收购信号：**")
            for s in biz_signals:
                title = _md_escape_title(str(s.get("title") or "(no title)"))
                url = str(s.get("url") or "").strip()
                lines.append(
                    f"- {title}" + (f" [📎 原文]({url})" if url else "")
                )
            lines.append("")
        bc = int(insider.get("buy_count") or 0)
        sc = int(insider.get("sell_count") or 0)
        tbv = insider.get("total_buy_value")
        tsv = insider.get("total_sell_value")
        lines.append("#### 💼 内部交易（FMP）")
        lines.append("")
        if int(insider.get("trade_count") or 0) == 0:
            lines.append("*暂无*")
        else:
            lines.append(
                f"- 买入：**{bc}** 笔，总价值 **{_fmt_trade_side_value(float(tbv) if isinstance(tbv, (int, float)) and tbv == tbv else None, bc)}**"
            )
            lines.append(
                f"- 卖出：**{sc}** 笔，总价值 **{_fmt_trade_side_value(float(tsv) if isinstance(tsv, (int, float)) and tsv == tsv else None, sc)}**"
            )
        lines.append("")
    return lines


def _step6_annual_financial_table(
    per_company: list[
        tuple[
            CompanyRecord,
            list[dict[str, Any]],
            dict[str, Any],
            int,
            bool,
        ]
    ],
    years: int = 3,
) -> list[str]:
    """Step6: 年度财务数据表格（最近3年），含Net Debt/Equity"""
    from research_automation.extractors.fmp_client import get_financials

    lines: list[str] = ["## Step 6｜财务数据（年度）", ""]

    company_data: list[tuple[str, str, list[Any]]] = []
    all_years: list[int] = []
    for rec, _signals, _insider, _below, _had in per_company:
        t = rec.ticker
        disp = company_display_name(t, rec.company_name)
        try:
            rows = get_financials(t, years=years)
        except Exception:
            logger.exception("Step6 年度财务拉取失败 ticker=%s", t)
            rows = []
        company_data.append((t, disp, rows))
        for r in rows:
            if r.year not in all_years:
                all_years.append(r.year)

    all_years = sorted(set(all_years), reverse=True)[:years]

    if not all_years:
        lines.append("*（FMP 年度数据不可用，请检查 FMP_API_KEY 或网络。）*")
        lines.append("")
        return lines

    def fmt_b(v: float | None) -> str:
        if v is None:
            return "—"
        return f"${v / 1e9:.2f}B"

    def fmt_pct(v: float | None) -> str:
        if v is None:
            return "—"
        return f"{v * 100:.1f}%"

    def fmt_nd(v: float | None) -> str:
        if v is None:
            return "—"
        return f"{v:.2f}x"

    year_headers = " | ".join(f"FY{y}" for y in all_years)
    sep_line = "|--------|" + "|".join(["--------"] * len(all_years)) + "|"

    for metric_name, metric_fn in [
        ("Revenue", lambda r: fmt_b(r.revenue)),
        ("Gross Margin", lambda r: fmt_pct(r.gross_margin)),
        ("EBITDA", lambda r: fmt_b(r.ebitda)),
        (
            "CAPEX",
            lambda r: fmt_b(abs(r.capex)) if r.capex is not None else "—",
        ),
        ("Net Debt/Equity", lambda r: fmt_nd(r.net_debt_to_equity)),
    ]:
        lines.append(f"### {metric_name}")
        lines.append("")
        lines.append(f"| Ticker | {year_headers} |")
        lines.append(sep_line)
        for t, _disp, rows in company_data:
            row_by_year = {r.year: r for r in rows}
            vals = " | ".join(
                metric_fn(row_by_year[y]) if y in row_by_year else "—"
                for y in all_years
            )
            lines.append(f"| {t} | {vals} |")
        lines.append("")

    lines.append("### Sector 汇总")
    lines.append("")
    lines.append(f"| 指标 | {year_headers} |")
    lines.append(sep_line)
    for metric_name, attr in [("Total Revenue", "revenue"), ("Total CAPEX", "capex")]:
        vals_out: list[str] = []
        for y in all_years:
            total = 0.0
            has = False
            for _, _, rows in company_data:
                row_by_year = {r.year: r for r in rows}
                if y in row_by_year:
                    v = getattr(row_by_year[y], attr)
                    if v is not None:
                        if attr == "capex":
                            total += abs(float(v))
                        else:
                            total += float(v)
                        has = True
            vals_out.append(fmt_b(total) if has else "—")
        lines.append(f"| **{metric_name}** | {' | '.join(vals_out)} |")
    lines.append("")
    lines.append(
        "*数据来源：FMP Annual Financials。Net Debt/Equity = (总债务-现金)/股东权益。*"
    )
    lines.append("")
    return lines


def _step6_financial_table(
    per_company: list[
        tuple[
            CompanyRecord,
            list[dict[str, Any]],
            dict[str, Any],
            int,
            bool,
        ]
    ],
    quarters: int = 6,
) -> list[str]:
    from research_automation.extractors.fmp_client import get_quarterly_financials

    lines: list[str] = ["## Step 6｜财务数据（季度）", ""]
    company_data: list[tuple[str, str, list[dict[str, Any]]]] = []
    for rec, _signals, _insider, _below, _had in per_company:
        t = rec.ticker
        disp = company_display_name(t, rec.company_name)
        try:
            rows = get_quarterly_financials(t, quarters=quarters)
        except Exception:
            logger.exception("Step6 季度财务拉取失败 ticker=%s", t)
            rows = []
        company_data.append((t, disp, rows))

    if not any(rows for _, _, rows in company_data):
        lines.append("*（FMP 季度数据不可用，请检查 FMP_API_KEY 或网络。）*")
        lines.append("")
        return lines

    all_quarters: list[str] = []
    seen: set[str] = set()
    for _, _, rows in company_data:
        for r in rows:
            q = r["quarter"]
            if q not in seen:
                seen.add(q)
                all_quarters.append(q)
    all_quarters.sort(reverse=True)
    display_quarters = all_quarters[:quarters]
    header = "| Ticker | " + " | ".join(display_quarters) + " |"
    sep = "|--------|" + "|".join(["--------"] * len(display_quarters)) + "|"

    for title, metric_key, fmt_fn in [
        ("Revenue（USD）", "revenue", _fmt_usd),
        ("Gross Margin（%）", "gross_margin", _fmt_pct),
        ("EBITDA（USD）", "ebitda", _fmt_usd),
        ("CAPEX（USD）", "capex", _fmt_usd),
        ("CAPEX 2P ROC", "capex_2p_roc", _fmt_roc),
    ]:
        lines.append(f"### {title}")
        lines.append("")
        lines.append(header)
        lines.append(sep)
        for t, _disp, rows in company_data:
            by_q = {r["quarter"]: r for r in rows}
            vals = [
                fmt_fn(by_q[q][metric_key]) if q in by_q else "—"
                for q in display_quarters
            ]
            lines.append(f"| {t} | " + " | ".join(vals) + " |")
        lines.append("")

    lines.append("### Sector 汇总")
    lines.append("")
    lines.append(header)
    lines.append(sep)
    for label, metric_key in [
        ("Total Revenue", "revenue"),
        ("Total CAPEX", "capex"),
    ]:
        totals = []
        for q in display_quarters:
            total, has = 0.0, False
            for _, _, rows in company_data:
                by_q = {r["quarter"]: r for r in rows}
                v = by_q[q][metric_key] if q in by_q else None
                if v is not None:
                    total += v
                    has = True
            totals.append(_fmt_usd(total) if has else "—")
        lines.append(f"| **{label}** | " + " | ".join(totals) + " |")
    lines.append("")
    lines.append(
        "*数据来源：FMP Ultimate 季度报表。CAPEX 已取绝对值。2P ROC = (t − t−2) / |t−2|。*"
    )
    lines.append("")
    return lines


def generate_six_step_sector_report(
    sector: str,
    days_back: int | None = None,
    *,
    relevance_threshold: int | None = None,
    report_stats: dict[str, Any] | None = None,
    earnings_cross_review: dict[str, Any] | None = None,
    quarters: list[str] | None = None,
    sector_watch_items: list[str] | None = None,
    force_refresh: bool = False,
) -> str:
    """六步结构行业报告。每次LLM调用只处理单家公司。"""
    from research_automation.core.sector_config import get_sector_watch_items
    from research_automation.services.report_cache import get_cached_report, save_report_cache

    sec = (sector or "").strip()
    db = _resolve_sector_news_days_back(days_back)
    thr = (
        int(relevance_threshold)
        if relevance_threshold is not None
        else _relevance_threshold_from_env()
    )
    thr = max(0, min(3, thr))

    if sector_watch_items is None:
        sector_watch_items = get_sector_watch_items(sec)

    if not sec:
        return "# 行业报告（六步结构）\n\n（sector 为空）\n"

    # ── 缓存读取 ──────────────────────────────────────────────
    now_utc = datetime.now(timezone.utc)
    cache_year = now_utc.year
    cache_quarter = (now_utc.month - 1) // 3 + 1
    if cache_quarter == 1:
        cache_quarter = 4
        cache_year -= 1
    else:
        cache_quarter -= 1
    if not force_refresh:
        cached = get_cached_report(sec, cache_year, cache_quarter)
        if cached:
            return cached
    # ── 缓存读取 END ──────────────────────────────────────────

    loaded = _load_per_company_signals_and_insiders(sec, db, thr, report_stats)
    if loaded is None:
        now = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M")
        return (
            f"# 行业报告（六步结构）：{sec}\n\n"
            f"**生成时间**：{now} UTC\n\n未找到该 sector 的活跃公司。\n"
        )

    _companies, per_company, _stats, _fetch_tot = loaded
    now = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M")
    lines: list[str] = [
        f"# 行业报告（六步结构）：{sec}",
        f"**生成时间**：{now} UTC",
        f"**新闻窗口**：最近 {int(db)} 个 UTC 日历日",
        "",
    ]
    lines.extend(_step1_sector_business_overview(per_company))
    lines.extend(_step2_per_company_revenue_breakdown(per_company))
    lines.extend(_step3_per_company_outlook(per_company))
    lines.extend(
        _step4_earning_call_section(
            sec,
            earnings_cross_review,
            quarters,
            sector_watch_items=sector_watch_items,
            per_company=per_company,
        )
    )
    lines.extend(_step5_new_biz_acquisitions_insider(per_company))
    lines.extend(_step6_annual_financial_table(per_company, years=3))
    # ── 缓存写入 ──────────────────────────────────────────────
    report_md = "\n".join(lines).rstrip() + "\n"
    save_report_cache(sec, cache_year, cache_quarter, report_md)
    return report_md
