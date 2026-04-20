"""按 ``sector`` 汇总 Tavily 新闻信号与 FMP 内部交易，生成 Markdown 行业报告。"""
from __future__ import annotations

import concurrent.futures
import logging
import os
import re
from concurrent.futures import ThreadPoolExecutor, as_completed
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
    max_workers: int = 8,
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
    """并行拉取所有公司的信号与内部交易数据。"""
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

    seen_urls_global: set[str] = set()
    raw_total = 0
    filtered_total = 0
    below_thr_total = 0
    dup_cross = 0

    def _fetch_one(
        rec: CompanyRecord,
    ) -> tuple[
        CompanyRecord,
        list[dict[str, Any]],
        dict[str, Any],
        int,
        bool,
        SignalFetchStats,
    ]:
        """单家公司：拉信号 + insider，返回 (rec, relevance_filtered, insider, below_n, had_signals, sym_stats)。"""
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

        had_signals = len(signals) > 0
        filtered, below_n = _filter_sort_signals(signals, thr)

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

        return rec, filtered, insider, below_n, had_signals, sym_stats

    results_map: dict[
        str,
        tuple[
            CompanyRecord,
            list[dict[str, Any]],
            dict[str, Any],
            int,
            bool,
            SignalFetchStats,
        ],
    ] = {}
    workers = max(1, min(int(max_workers), 32))
    with ThreadPoolExecutor(max_workers=workers) as executor:
        future_to_ticker = {
            executor.submit(_fetch_one, rec): rec.ticker for rec in companies
        }
        for future in as_completed(future_to_ticker):
            ticker = future_to_ticker[future]
            try:
                results_map[ticker] = future.result()
            except Exception:
                logger.exception("并行拉取异常 ticker=%s", ticker)

    per_company: list[
        tuple[
            CompanyRecord,
            list[dict[str, Any]],
            dict[str, Any],
            int,
            bool,
        ]
    ] = []
    for rec in companies:
        if rec.ticker not in results_map:
            continue
        rec_out, filtered, insider, below_n, had_signals, sym_stats = results_map[
            rec.ticker
        ]

        fetch_tot.merge_from(sym_stats)
        raw_total += len(filtered) + below_n
        below_thr_total += below_n

        deduped: list[dict[str, Any]] = []
        for s in filtered:
            u = (str(s.get("url") or "")).strip().lower()
            if u:
                if u in seen_urls_global:
                    dup_cross += 1
                    continue
                seen_urls_global.add(u)
            deduped.append(s)

        filtered_total += len(deduped)
        per_company.append((rec_out, deduped, insider, below_n, had_signals))

    stats.update(
        {
            "relevance_threshold": thr,
            "raw_signal_count": raw_total,
            "filtered_signal_count": filtered_total,
            "below_relevance_dropped": below_thr_total,
            "cross_ticker_duplicate_urls": dup_cross,
            "fetch_aggregate": fetch_tot,
        }
    )
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


def _step0_sector_summary(
    sector: str,
    per_company: list[
        tuple[
            CompanyRecord,
            list[dict[str, Any]],
            dict[str, Any],
            int,
            bool,
        ]
    ],
    sector_watch_items: list[str] | None = None,
) -> list[str]:
    """Step0：Sector 整体总结段，LLM基于财务数据和公司列表生成。"""
    from research_automation.extractors.fmp_client import get_financials
    from research_automation.extractors.llm_client import chat

    lines: list[str] = ["## Sector 总览", ""]

    # 收集各公司最新财务数据
    company_snapshots: list[str] = []
    for rec, _signals, _insider, _below, _had in per_company:
        t = rec.ticker
        disp = company_display_name(t, rec.company_name)
        try:
            financials = get_financials(t, years=2)
            if not financials:
                continue
            latest = max(financials, key=lambda r: getattr(r, "year", 0) or 0)
            prev_list = [r for r in financials if (getattr(r, "year", 0) or 0) < (getattr(latest, "year", 0) or 0)]
            prev = max(prev_list, key=lambda r: getattr(r, "year", 0) or 0) if prev_list else None

            rev = getattr(latest, "revenue", None)
            if rev is None:
                continue
            prev_rev = getattr(prev, "revenue", None) if prev else None
            rev_growth = (
                (float(rev) - float(prev_rev)) / abs(float(prev_rev)) * 100
                if rev and prev_rev and prev_rev != 0 else None
            )
            gm = getattr(latest, "gross_margin", None)
            ebitda = getattr(latest, "ebitda", None)

            snap = f"{t}（{disp}）：Revenue ${float(rev)/1e9:.1f}B"
            if rev_growth is not None:
                snap += f" YoY {rev_growth:+.1f}%"
            if gm is not None:
                gm_val = float(gm) * 100 if float(gm) < 2 else float(gm)
                snap += f"，Gross Margin {gm_val:.1f}%"
            if ebitda is not None:
                snap += f"，EBITDA ${float(ebitda)/1e9:.1f}B"
            company_snapshots.append(snap)
        except Exception:
            logger.exception("Step0 财务快照失败 ticker=%s", t)
            continue

    if not company_snapshots:
        lines.append("*（财务数据不可用，无法生成sector总结）*")
        lines.append("")
        return lines

    watch_str = "、".join(sector_watch_items) if sector_watch_items else "无"
    snapshot_text = "\n".join(f"- {s}" for s in company_snapshots)

    prompt = f"""你是资深行业研究分析师。以下是{sector}板块各公司最新财务快照：

{snapshot_text}

本sector重点关注项：{watch_str}

直接输出正文段落，不要输出任何标题或##开头的行。

请写一段精准的sector整体总结，要求：
1. 必须包含具体数字（收入规模、增长率、利润率）
2. 点出本季度sector最突出的1-2个共同趋势，附具体公司名称和数据
3. 指出哪些公司表现明显优于或差于sector平均，说明原因
4. 提及管理层普遍关注的前瞻性因素或风险
5. 只陈述可验证的事实，不做投资建议
6. 中文输出，公司名/指标保留英文，长度控制在150-250字"""

    try:
        summary = chat(prompt, max_tokens=600)
        lines.append(summary)
        lines.append("")
    except Exception:
        logger.exception("Step0 sector总结LLM调用失败")
        lines.append("*（sector总结生成失败）*")
        lines.append("")

    return lines


def _step0b_company_snapshot_table(
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
    """个股快速扫描表：一张表横向对比所有公司关键指标。"""
    from research_automation.extractors.fmp_client import get_financials

    lines: list[str] = ["## 个股快速扫描（最新财年）", ""]
    lines.append("| Ticker | Revenue | YoY | Gross Margin | EBITDA | Net Debt/Eq | 本周关键信号 |")
    lines.append("|--------|---------|-----|--------------|--------|-------------|------------|")

    for rec, signals, _insider, _below, _had in per_company:
        t = rec.ticker
        try:
            financials = get_financials(t, years=2)
            if not financials:
                lines.append(f"| {t} | — | — | — | — | — | — |")
                continue

            latest = max(financials, key=lambda r: getattr(r, "year", 0) or 0)
            prev_list = [r for r in financials if (getattr(r, "year", 0) or 0) < (getattr(latest, "year", 0) or 0)]
            prev = max(prev_list, key=lambda r: getattr(r, "year", 0) or 0) if prev_list else None

            rev = getattr(latest, "revenue", None)
            if rev is None:
                lines.append(f"| {t} | — | — | — | — | — | — |")
                continue

            prev_rev = getattr(prev, "revenue", None) if prev else None
            rev_growth = (
                (float(rev) - float(prev_rev)) / abs(float(prev_rev)) * 100
                if prev_rev and prev_rev != 0 else None
            )
            gm = getattr(latest, "gross_margin", None)
            ebitda = getattr(latest, "ebitda", None)
            nd_eq = getattr(latest, "net_debt_to_equity", None)

            def fmt_b(v):
                if v is None: return "—"
                x = float(v)
                return f"${x/1e9:.1f}B" if abs(x) >= 1e9 else f"${x/1e6:.0f}M"

            def fmt_pct(v):
                if v is None: return "—"
                val = float(v) * 100 if float(v) < 2 else float(v)
                if val > 95 or val < -50:
                    return "—"
                return f"{val:.1f}%"

            def fmt_nd(v):
                if v is None: return "—"
                return f"{float(v):.1f}x"

            yoy = f"{rev_growth:+.1f}%" if rev_growth is not None else "—"

            # 本周关键信号：取最高相关性的1条新闻标题
            top_signal = ""
            if signals:
                top = max(signals, key=lambda s: int(s.get("relevance_score") or 0))
                title = str(top.get("title") or "")[:30]
                top_signal = title + ("…" if len(str(top.get("title") or "")) > 30 else "")

            lines.append(
                f"| {t} | {fmt_b(rev)} | {yoy} | {fmt_pct(gm)} | {fmt_b(ebitda)} | {fmt_nd(nd_eq)} | {top_signal} |"
            )
        except Exception:
            logger.exception("Step0b 快速扫描失败 ticker=%s", t)
            lines.append(f"| {t} | — | — | — | — | — | — |")

    lines.append("")
    lines.append("*数据来源：FMP Annual Financials（最新财年）。本周关键信号来自 Benzinga。*")
    lines.append("")
    return lines


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
            lines.append(f"- {seg['segment']}: {seg['percentage']:.1f}%　(${seg['absolute']/1e9:.2f}B)")
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
    from research_automation.extractors.fmp_client import (
        get_geographic_revenue,
        get_segment_revenue,
    )
    lines: list[str] = ["## Step 2｜业务占比（产品线 + 地理收入）", ""]

    # ── Sector 级别业务占比总结（纯数据，无LLM）──────────────────
    from research_automation.extractors.fmp_client import get_segment_revenue, get_geographic_revenue

    # 收集所有公司产品线数据
    all_segments: dict[str, float] = {}
    all_geo: dict[str, float] = {}
    total_rev = 0.0
    covered = 0

    for rec, *_ in per_company:
        t = rec.ticker
        data = get_segment_revenue(t, 2024) or get_segment_revenue(t, 2023)
        if not data:
            continue
        covered += 1
        rev_total = sum(d["absolute"] for d in data)
        total_rev += rev_total
        for seg in data:
            key = seg["segment"]
            all_segments[key] = all_segments.get(key, 0) + seg["absolute"]

        geo = get_geographic_revenue(t, 2024) or get_geographic_revenue(t, 2023)
        if geo:
            for g in geo:
                key = g["region"]
                all_geo[key] = all_geo.get(key, 0) + g["absolute"]

    if covered > 0 and total_rev > 0:
        lines.append(f"> **数据来源**：FMP Revenue Segmentation | **覆盖**：{covered}/{len(per_company)} 家公司 | **合计收入**：${total_rev/1e9:.1f}B")
        lines.append("")

        # 产品线 Top5
        top_segs = sorted(all_segments.items(), key=lambda x: x[1], reverse=True)[:5]
        lines.append("**板块收入结构（产品线 Top 5，按绝对收入排序）：**")
        lines.append("")
        for seg_name, seg_rev in top_segs:
            pct = seg_rev / total_rev * 100
            lines.append(f"- {seg_name}：{pct:.1f}%（${seg_rev/1e9:.1f}B）")
        lines.append("")

        # 地理 Top5
        if all_geo:
            geo_total = sum(all_geo.values())
            top_geo = sorted(all_geo.items(), key=lambda x: x[1], reverse=True)[:5]
            lines.append("**板块地理收入分布（Top 5）：**")
            lines.append("")
            for geo_name, geo_rev in top_geo:
                pct = geo_rev / geo_total * 100
                lines.append(f"- {geo_name}：{pct:.1f}%（${geo_rev/1e9:.1f}B）")
            lines.append("")

        lines.append("<!--- COMPANY_DETAILS_START --->")
        lines.append("")
    # ── Sector 总结 END ──────────────────────────────────────────

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

        # 地理收入拆分
        geo_data = get_geographic_revenue(t, year_used)
        if geo_data:
            lines.append(f"**地理收入拆分（FY{year_used}）：**")
            lines.append("")
            lines.append("| 地区 | 占比 | 收入 |")
            lines.append("|------|------|------|")
            for g in geo_data:
                lines.append(
                    f"| {g['region']} | {g['percentage']:.1f}% | "
                    f"${g['absolute']/1e9:.2f}B |"
                )
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
    *,
    max_workers: int = 8,
) -> list[str]:
    from research_automation.services.profile_service import (
        ProfileGenerationError,
        get_profile,
    )

    def _one_company_block(
        row: tuple[
            CompanyRecord,
            list[dict[str, Any]],
            dict[str, Any],
            int,
            bool,
        ],
    ) -> tuple[str, list[str]]:
        rec, _signals, _insider, _below, _had = row
        t = rec.ticker
        disp = company_display_name(t, rec.company_name)
        chunk: list[str] = [
            f"### {t} — {disp}",
            "",
        ]
        try:
            profile = get_profile(t)
            fg = (profile.future_guidance or "").strip()
            if fg and fg not in ("原文未明确提及", "NOT_FOUND"):
                chunk.extend(
                    [
                        "**未来展望与指引：**",
                        "",
                        fg,
                        "",
                    ]
                )
            else:
                chunk.extend(["**未来展望与指引：** *原文未明确提及*", ""])
            iv = (profile.industry_view or "").strip()
            if iv and iv not in ("原文未明确提及", "NOT_FOUND"):
                chunk.extend(
                    [
                        "**行业判断（管理层视角）：**",
                        "",
                        iv,
                    ]
                )
                if profile.industry_view_source:
                    chunk.append(f"*原文依据：{profile.industry_view_source}*")
                chunk.append("")
            else:
                chunk.extend(
                    ["**行业判断（管理层视角）：** *原文未明确提及*", ""]
                )
            fwd_quotes = [
                q
                for q in (profile.key_quotes or [])
                if getattr(q, "modality", "") == "forward_looking"
            ]
            if fwd_quotes:
                chunk.extend(["**前瞻性原话：**", ""])
                for q in fwd_quotes[:3]:
                    chunk.append(f"> **{q.speaker or 'UNKNOWN'}**：\"{q.quote}\"")
                    chunk.append(f"> *主题：{q.topic}*")
                    chunk.append("")
        except ProfileGenerationError as e:
            chunk.append(f"*（画像生成失败：{e.message}）*")
            chunk.append("")
        except Exception:
            logger.exception("Step3 get_profile 失败 ticker=%s", t)
            chunk.append("*（画像拉取失败，详见日志）*")
            chunk.append("")
        return t, chunk

    lines: list[str] = ["## Step 3｜展望与战略重心", ""]
    if not per_company:
        return lines

    results_map: dict[str, list[str]] = {}
    workers = max(1, min(int(max_workers), 32))
    with ThreadPoolExecutor(max_workers=workers) as executor:
        future_to_ticker = {
            executor.submit(_one_company_block, row): row[0].ticker
            for row in per_company
        }
        for future in as_completed(future_to_ticker):
            tk = future_to_ticker[future]
            try:
                ticker, chunk = future.result()
                results_map[ticker] = chunk
            except Exception:
                logger.exception("Step3 并行任务异常 ticker=%s", tk)

    # ── Sector 级别展望总结（LLM）────────────────────────────
    from research_automation.extractors.llm_client import chat as _chat3

    # 收集各公司 future_guidance 和 industry_view
    outlook_briefs: list[str] = []
    for rec, *_ in per_company:
        blk = results_map.get(rec.ticker, [])
        if not blk:
            continue
        blk_text = '\n'.join(blk)
        if '原文未明确提及' in blk_text and 'NOT_FOUND' in blk_text:
            continue
        # 只取前300字
        outlook_briefs.append(f"【{rec.ticker}】\n{blk_text[:300]}")

    if len(outlook_briefs) >= 2:
        briefs_text = '\n\n'.join(outlook_briefs)
        prompt = f"""你是资深行业研究分析师。以下是{sector if hasattr(locals(), 'sector') else '该'}板块各公司管理层对未来的展望与行业判断：

{briefs_text}

请生成板块整体展望总结。要求：
1. 只提炼多家公司（至少2家）共同提到的战略方向或行业判断
2. 每条结论必须标注具体公司名称，禁止模糊表述
3. 分点列出，每点以【战略方向】或【行业判断】开头
4. 单家公司独有的展望不在此列出，留给个股详情
5. 每句必须有实质内容，禁止车轱辘话
6. 数据来源：各公司10-K及Earning Call（SEC EDGAR / FMP）
7. 中文输出，公司名保留英文
8. 禁止输出任何以#开头的标题行，直接输出正文段落"""

        try:
            sector_outlook = _chat3(prompt, max_tokens=600)
            lines.append("> **数据来源**：各公司 10-K 及 Earning Call（SEC EDGAR / FMP）｜**评判标准**：至少2家公司共同提及的战略方向或行业判断")
            lines.append("> ⚠️ **以下为系统辅助总结，非原文直接提取，仅供参考。详细原文请展开各公司详情查看。**")
            lines.append("")
            lines.append(sector_outlook)
            lines.append("")

            # Reference 块
            lines.append("**参考来源：**")
            lines.append("")
            for rec, *_ in per_company:
                blk = results_map.get(rec.ticker, [])
                if not blk:
                    continue
                blk_text = '\n'.join(blk)
                if len(blk_text.strip()) < 50:
                    continue
                has_guidance = "未来展望" in blk_text and "原文未明确提及" not in blk_text[:100]
                has_industry = "行业判断" in blk_text and "原文未明确提及" not in blk_text[:200]
                if not has_guidance and not has_industry:
                    continue
                filing_year = __import__('datetime').datetime.now().year - 1
                content_types = []
                if has_guidance:
                    content_types.append("未来展望/指引")
                if has_industry:
                    content_types.append("行业判断")
                lines.append(
                    f"- **{rec.company_name or rec.ticker} ({rec.ticker})**："
                    f"10-K FY{filing_year} + Earning Call，来源：SEC EDGAR / FMP｜"
                    f"内容：{', '.join(content_types)}"
                )
            lines.append("")
            lines.append("<!--- COMPANY_DETAILS_START --->")
            lines.append("")
        except Exception:
            logger.exception("Step3 sector总结失败")
    # ── Sector 总结 END ──────────────────────────────────────

    for rec, *_rest in per_company:
        blk = results_map.get(rec.ticker)
        if blk:
            lines.extend(blk)
        else:
            t = rec.ticker
            disp = company_display_name(t, rec.company_name)
            lines.extend(
                [
                    f"### {t} — {disp}",
                    "",
                    "*（画像段落未生成：并行任务无结果）*",
                    "",
                ]
            )
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
        from concurrent.futures import ThreadPoolExecutor, as_completed

        from research_automation.models.earnings import EarningsCallAnalysis

        # LLM 限流保护：最多同时 4 个并发
        MAX_WORKERS = 4

        def _fetch_one(
            rec: CompanyRecord,
        ) -> tuple[str, Any]:
            """返回 (ticker, analysis_or_exception)"""
            t = rec.ticker
            try:
                result = analyze_earnings_call(
                    t,
                    year,
                    quarter,
                    sector_watch_items=sector_watch_items,
                )
                return t, result
            except EarningsAnalysisError as e:
                return t, e
            except Exception as exc:
                logger.exception("Step4 earnings 失败 ticker=%s", t)
                return t, exc

        # 并行拉取，保留原始顺序
        tickers_in_order = [rec.ticker for rec, *_ in per_company]
        rec_map = {rec.ticker: rec for rec, *_ in per_company}

        results: dict[str, Any] = {}
        with ThreadPoolExecutor(max_workers=MAX_WORKERS) as pool:
            future_to_ticker = {
                pool.submit(_fetch_one, rec_map[t]): t for t in tickers_in_order
            }
            for future in as_completed(future_to_ticker):
                ticker, outcome = future.result()
                results[ticker] = outcome

        # ── Sector 级别 Earning Call 总结 ──────────────────────────
        # 收集所有成功分析的结果，生成跨公司总结
        successful_analyses: list[tuple[str, Any]] = [
            (t, results[t]) for t in tickers_in_order
            if isinstance(results.get(t), EarningsCallAnalysis)
        ]

        if successful_analyses:
            from research_automation.extractors.llm_client import chat as _chat

            # 抽取每家公司的概括和关键观点（控制token）
            company_briefs: list[str] = []
            for t, analysis in successful_analyses:
                brief_parts = [f"【{t}】"]
                if analysis.summary:
                    brief_parts.append(f"概括：{analysis.summary[:200]}")
                if analysis.management_viewpoints:
                    vp_texts = [vp.text for vp in analysis.management_viewpoints[:3]]
                    brief_parts.append(f"核心观点：{'；'.join(vp_texts)}")
                if analysis.quotations:
                    q = analysis.quotations[0]
                    brief_parts.append(f"关键原话：{q.speaker}：\"{q.quote[:100]}\"")
                company_briefs.append('\n'.join(brief_parts))

            watch_str = '、'.join(sector_watch_items) if sector_watch_items else '无'
            briefs_text = '\n\n'.join(company_briefs)

            sector_summary_prompt = f"""你是资深行业研究分析师。以下是{_sector}板块本季度各公司Earning Call的关键内容：

{briefs_text}

本sector重点关注项：{watch_str}

请生成一份{_sector}板块本季度Earning Call总结。

要求：
1. 每一条结论必须有具体公司名称和具体数字支撑，禁止泛泛而谈
2. 只提炼在多家公司中同时出现的共同趋势，单家公司独有的信息放到个股详情里
3. 重点关注：sector_watch_items中列出的关注项在各公司的实际表现
4. 如果某个趋势只有1家公司提到，不要单独列出，归入个股详情
5. 格式：分点列出，每点以【趋势名称】开头，后跟具体公司数据
6. 长度不限，但每句必须有实质信息，禁止车轱辘话
7. 数据来源：各公司Earning Call逐字稿（FMP/SEC EDGAR）
8. 中文输出，公司名/数字/人名保留英文
9. 禁止输出任何以#开头的标题行，直接输出正文段落"""

            try:
                sector_summary = _chat(sector_summary_prompt, max_tokens=1200)
                lines.append("> **数据来源**：各公司 Earning Call 逐字稿（FMP / SEC EDGAR）｜**评判标准**：跨3家以上公司出现的共同表述，每条结论附具体数字")
                lines.append("> ⚠️ **以下为系统辅助总结，非原文直接提取，仅供参考。原文 quotations 请展开各公司详情查看。**")
                lines.append("")
                lines.append(sector_summary)
                lines.append("")

                # Reference 块
                lines.append("**参考来源：**")
                lines.append("")
                for t, analysis in successful_analyses:
                    rec = rec_map.get(t)
                    company_name = rec.company_name if rec else t
                    q_label = f"{year}Q{quarter}"
                    source = "FMP API"
                    # 列出该公司被引用的关键数据点
                    data_points = []
                    if analysis.summary:
                        # 提取数字（简单正则）
                        import re as _re4

                        numbers = _re4.findall(r'\$[\d,\.]+[BMK]?|\d+\.?\d*%|\d+,?\d+', analysis.summary[:300])
                        if numbers:
                            data_points.append(f"关键数据：{', '.join(numbers[:5])}")
                    if analysis.quotations:
                        data_points.append(f"Quotations：{len(analysis.quotations)} 条原话")
                    if analysis.management_viewpoints:
                        data_points.append(f"管理层观点：{len(analysis.management_viewpoints)} 条")
                    ref_line = f"- **{company_name} ({t})**：Earning Call 逐字稿 {q_label}，来源：{source}"
                    if data_points:
                        ref_line += f"｜{' ｜ '.join(data_points)}"
                    lines.append(ref_line)
                lines.append("")
                lines.append("<!--- COMPANY_DETAILS_START --->")
                lines.append("")
            except Exception:
                logger.exception("Step4 sector总结失败")
        # ── Sector 级别总结 END ──────────────────────────────────────

        has_any = False
        for t in tickers_in_order:
            rec = rec_map[t]
            disp = company_display_name(t, rec.company_name)
            lines.append(f"### {t} — {disp}")
            lines.append("")
            outcome = results.get(t)
            if outcome is None:
                lines.append("*（无分析结果）*")
                lines.append("")
            elif isinstance(outcome, EarningsAnalysisError):
                lines.append(f"*（逐字稿不可用：{outcome.message}）*")
                lines.append("")
            elif isinstance(outcome, Exception):
                lines.append("*（分析失败，详见日志）*")
                lines.append("")
            elif isinstance(outcome, EarningsCallAnalysis):
                analysis = outcome
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

    # ── Sector 级别新业务/收购总结（LLM）────────────────────────
    from research_automation.extractors.llm_client import chat as _chat5

    # 收集所有公司的业务信号
    all_signals_brief: list[str] = []
    for rec, signals, insider, _below, _had in per_company:
        t = rec.ticker
        biz_signals = [
            s for s in signals
            if str(s.get("signal_type") or "") in ("business_change", "insider_trade")
        ]
        insider_count = int(insider.get("trade_count") or 0)
        if not biz_signals and insider_count == 0:
            continue
        parts = [f"【{t}】"]
        for s in biz_signals[:3]:
            title = str(s.get("title") or "")[:100]
            parts.append(f"- {title}")
        if insider_count > 0:
            bc = int(insider.get("buy_count") or 0)
            sc = int(insider.get("sell_count") or 0)
            tbv = insider.get("total_buy_value")
            tsv = insider.get("total_sell_value")
            insider_str = f"Insider：买入{bc}笔"
            if tbv:
                insider_str += f"（${float(tbv)/1e6:.1f}M）"
            insider_str += f"，卖出{sc}笔"
            if tsv:
                insider_str += f"（${float(tsv)/1e6:.1f}M）"
            parts.append(f"- {insider_str}")
        all_signals_brief.append('\n'.join(parts))

    if len(all_signals_brief) >= 1:
        briefs_text = '\n\n'.join(all_signals_brief)
        prompt = f"""你是资深行业研究分析师。以下是板块各公司本周的新业务、收购并购及Insider交易信号：

{briefs_text}

请生成板块整体新业务与Insider动态总结。要求：
1. 重点标注：大额收购、战略合作、管理层大额买入/卖出
2. 每条结论必须有具体公司名称、金额或事件名称
3. Insider交易：只标注异常的（买入超过$1M或卖出超过$5M）
4. 分点列出，格式：[公司] 事件描述（金额/规模）
5. 如果本周信号较少，如实说明，不要凑字数
6. 数据来源：Benzinga公司新闻 + FMP Insider交易申报
7. 中文输出，公司名/金额保留英文
8. 禁止输出任何以#开头的标题行，直接输出正文段落"""

        try:
            sector_signal = _chat5(prompt, max_tokens=400)
            lines.append("> **数据来源**：Benzinga 公司新闻 + FMP Insider 交易申报（Form 4）｜**评判标准**：收购/战略合作/异常 Insider 交易（买入>$1M 或卖出>$5M）")
            lines.append("> ⚠️ **以下为系统辅助总结，非原文直接提取，仅供参考。原文链接请展开各公司详情查看。**")
            lines.append("")
            lines.append(sector_signal)
            lines.append("")

            # Reference 块
            lines.append("**参考来源：**")
            lines.append("")
            for rec, signals, insider, _below, _had in per_company:
                t = rec.ticker
                biz_signals = [
                    s for s in signals
                    if str(s.get("signal_type") or "") in ("business_change", "insider_trade")
                ]
                insider_count = int(insider.get("trade_count") or 0)
                if not biz_signals and insider_count == 0:
                    continue
                ref_parts = []
                if biz_signals:
                    ref_parts.append(f"Benzinga 新闻 {len(biz_signals)} 条")
                if insider_count > 0:
                    bc = int(insider.get("buy_count") or 0)
                    sc = int(insider.get("sell_count") or 0)
                    ref_parts.append(f"FMP Insider 申报：买入{bc}笔/卖出{sc}笔（Form 4）")
                lines.append(
                    f"- **{rec.company_name or t} ({t})**："
                    f"{' ｜ '.join(ref_parts)}"
                )
            lines.append("")
            lines.append("<!--- COMPANY_DETAILS_START --->")
            lines.append("")
        except Exception:
            logger.exception("Step5 sector总结失败")
    # ── Sector 总结 END ──────────────────────────────────────────

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


def _executive_summary(
    sector: str,
    step4_lines: list[str],
    step5_lines: list[str],
    step6_lines: list[str],
    sector_watch_items: list[str] | None = None,
    per_company: list | None = None,
) -> list[str]:
    """执行摘要：汇总Earning Call、新业务、财务数据，生成sector级别的执行摘要。"""
    from research_automation.extractors.llm_client import chat

    # 抽取关键内容（控制token）
    def _extract_key_lines(lines: list[str], max_lines: int = 60) -> str:
        # 过滤掉空行和纯格式行，保留实质内容
        filtered = [
            l for l in lines
            if l.strip() and not l.strip().startswith("|---")
        ]
        return "\n".join(filtered[:max_lines])

    step4_text = _extract_key_lines(step4_lines, 80)
    step5_text = _extract_key_lines(step5_lines, 30)
    step6_text = _extract_key_lines(step6_lines, 40)

    watch_str = "、".join(sector_watch_items) if sector_watch_items else "无"

    # 收集财务快照
    financial_snapshot = ""
    if per_company:
        from research_automation.extractors.fmp_client import get_financials
        import statistics as _stats

        yoy_list, gm_list = [], []
        company_snaps = []
        for rec, *_ in per_company:
            try:
                rows = get_financials(rec.ticker, years=2)
                if not rows or len(rows) < 2:
                    continue
                latest = max(rows, key=lambda r: getattr(r, "year", 0) or 0)
                prev_list = [r for r in rows if (getattr(r, "year", 0) or 0) < (getattr(latest, "year", 0) or 0)]
                prev = max(prev_list, key=lambda r: getattr(r, "year", 0) or 0) if prev_list else None
                rev = getattr(latest, "revenue", None)
                prev_rev = getattr(prev, "revenue", None) if prev else None
                if rev and prev_rev and prev_rev != 0:
                    yoy = (float(rev) - float(prev_rev)) / abs(float(prev_rev)) * 100
                    yoy_list.append(yoy)
                    company_snaps.append(f"{rec.ticker}: YoY {yoy:+.1f}%")
                gm = getattr(latest, "gross_margin", None)
                if gm is not None:
                    gm_val = float(gm) * 100 if float(gm) < 2 else float(gm)
                    if 0 < gm_val < 95:
                        gm_list.append(gm_val)
            except Exception:
                continue
        if yoy_list:
            median_yoy = _stats.median(yoy_list)
            median_gm = _stats.median(gm_list) if gm_list else None
            top3 = sorted(company_snaps, reverse=True)[:3]
            bot3 = sorted(company_snaps)[:3]
            gm_disp = f"{median_gm:.1f}%" if median_gm is not None else "—"
            financial_snapshot = f"""
Sector财务快照：
- 中位Revenue YoY：{median_yoy:+.1f}%
- 中位Gross Margin：{gm_disp}
- 增速前三：{", ".join(top3)}
- 增速后三：{", ".join(bot3)}
"""

    prompt = f"""你是资深行业研究分析师。以下是{sector}板块本季度完整数据：

【财务快照】
{financial_snapshot}

【Earning Call 摘录】
{step4_text}

【新业务/收购/Insider 摘录】
{step5_text}

【财务数据摘录】
{step6_text}

本sector重点关注项：{watch_str}

请生成一份执行摘要，严格按以下格式输出：

### 📊 财务快照
> 数据来源：FMP Annual Financials | 评判标准：最新财年Revenue YoY增速中位数及分布
（sector整体增速中位数、头尾公司对比、Gross Margin水平，2-3句，必须有具体数字）

### 🔑 本季核心主题
> 数据来源：Earning Call 逐字稿（FMP/SEC EDGAR）| 评判标准：跨3家以上公司出现的共同表述
（sector级别最重要的1-3个共同趋势，每个趋势必须附2个以上公司名称和具体数据）

### ⚡ 重要事件
> 数据来源：Earning Call 逐字稿 + Benzinga 公司新闻 | 评判标准：涉及资本配置/人员/产品的实质性变化
（本季最值得关注的3-5个具体事件，格式：[公司] 事件描述，按重要性排序）

### 💬 管理层关键信号
> 数据来源：Earning Call 逐字稿原话 | 评判标准：CEO/CFO对业务趋势的直接表态
（跨公司共同表态或分歧，附具体发言人姓名和原话关键词，2-3条）

### ⚠️ 主要风险
> 数据来源：Earning Call 前瞻性表述（含 expect/may/consider 等情态动词）| 评判标准：管理层主动披露的不确定因素
（管理层普遍提及的风险，附具体公司和表述，2-3条）

要求：
1. 财务快照和本季核心主题不得重复相同内容
2. 每个板块必须有具体公司名称、数字、事件
3. 只基于提供的原文，不捏造信息
4. 中文输出，公司名/指标/人名保留英文
5. 总长度控制在350-450字"""

    try:
        summary = chat(prompt, max_tokens=1200)
    except Exception:
        logger.exception("执行摘要LLM调用失败")
        return []

    # 生成 Reference 块
    ref_lines = ["**参考来源：**", ""]
    ref_lines.append("| 板块 | 数据来源 | 覆盖范围 |")
    ref_lines.append("|------|---------|---------|")
    ref_lines.append("| 📊 财务快照 | FMP Annual Financials API | 最新财年 Revenue YoY、Gross Margin，覆盖全部可用美股标的 |")
    ref_lines.append("| 🔑 本季核心主题 | Earning Call 逐字稿（FMP / SEC EDGAR） | 跨3家以上公司出现的共同表述，每条结论附具体数字 |")
    ref_lines.append("| ⚡ 重要事件 | Earning Call 逐字稿 + Benzinga 公司新闻 | 涉及资本配置/人员/产品的实质性变化 |")
    ref_lines.append("| 💬 管理层关键信号 | Earning Call 逐字稿原话 | CEO/CFO 直接表态，附发言人姓名 |")
    ref_lines.append("| ⚠️ 主要风险 | Earning Call 前瞻性表述（含 expect/may/consider） | 管理层主动披露的不确定因素 |")
    ref_lines.append("")

    if per_company:
        ref_lines.append("**涉及公司：**")
        ref_lines.append("")
        covered = []
        for rec, *_ in per_company:
            covered.append(f"{rec.ticker}（{rec.company_name or rec.ticker}）")
        ref_lines.append("、".join(covered))
        ref_lines.append("")

    lines_out: list[str] = [
        "## 📋 执行摘要",
        "",
        "> ⚠️ **以下执行摘要为系统基于财务数据及Earning Call逐字稿的辅助总结，非原文直接提取，仅供参考。各项结论的原始依据请查阅各Step详情中的quotations及数据来源。**",
        "",
        summary,
        "",
        "\n".join(ref_lines),
        "",
        "---",
        "",
    ]
    return lines_out


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
    # Step0（LLM）与 Step1/2/5/6 串行；Step1/2/5/6 无 LLM 调用
    lines.extend(_step0b_company_snapshot_table(per_company))
    lines.extend(_step2_per_company_revenue_breakdown(per_company))

    # Step3/4 有 LLM 调用，并行执行
    with ThreadPoolExecutor(max_workers=2) as executor:
        f3 = executor.submit(_step3_per_company_outlook, per_company)
        f4 = executor.submit(
            _step4_earning_call_section,
            sec,
            earnings_cross_review,
            quarters,
            sector_watch_items,
            per_company,
        )
        step3_lines = f3.result()
        step4_lines = f4.result()

    step5_lines = _step5_new_biz_acquisitions_insider(per_company)
    step6_lines = _step6_annual_financial_table(per_company, years=3)

    # 生成执行摘要（需要step4/5/6内容）
    exec_summary_lines = _executive_summary(
        sec, step4_lines, step5_lines, step6_lines, sector_watch_items, per_company
    )

    # 执行摘要插入报告最前面（header之后）
    header_lines = lines[:4]  # # 标题、生成时间、新闻窗口、空行
    body_lines = lines[4:]
    lines = header_lines + exec_summary_lines + body_lines

    lines.extend(step3_lines)
    lines.extend(step4_lines)
    lines.extend(step5_lines)
    lines.extend(step6_lines)
    # ── 缓存写入 ──────────────────────────────────────────────
    report_md = "\n".join(lines).rstrip() + "\n"
    save_report_cache(sec, cache_year, cache_quarter, report_md)
    return report_md
