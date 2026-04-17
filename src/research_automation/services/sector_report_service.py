"""按 ``sector`` 汇总 Tavily 新闻信号与 FMP 内部交易，生成 Markdown 行业报告。"""
from __future__ import annotations

import logging
import os
import re
from datetime import datetime, timezone
from typing import Any

from research_automation.core.company_manager import CompanyRecord, list_companies
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
