"""按 ``sector`` 汇总 Tavily 新闻信号与 FMP 内部交易，生成 Markdown 行业报告。"""
from __future__ import annotations

import concurrent.futures
import json
import logging
import os
import re
import threading
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import date, datetime, timedelta, timezone
from typing import Any

from research_automation.core.company_manager import CompanyRecord, list_companies
from research_automation.extractors.fmp_client import get_insider_trades
from research_automation.services.checker_config import SOFTWARE_EXCEPTIONS
from research_automation.services.insider_service import get_insider_summary
from research_automation.services.signal_fetcher import (
    SignalFetchStats,
    company_display_name,
    fetch_signals_for_ticker,
)

logger = logging.getLogger(__name__)


def _strict_expose_llm_errors() -> bool:
    """调试开关：``SECTOR_REPORT_STRICT_LLM=1`` 时板块级 LLM 失败不再吞掉异常，便于看到完整栈。"""
    return (os.getenv("SECTOR_REPORT_STRICT_LLM") or "").strip().lower() in (
        "1",
        "true",
        "yes",
    )


def _is_truncated_llm_output(text: str) -> bool:
    """空输出或句末非完整结束标点则视为可能被硬截断，可触发续写。
    含中文分点链常以「；」收束，勿误判为截断。"""
    t = (text or "").rstrip()
    if not t:
        return True
    return t[-1] not in (
        "。",
        "！",
        "？",
        ".",
        "!",
        "?",
        "\"",
        "”",
        "）",
        ")",
        "；",
        "…",
    )


_COMMON_ABBREVS: set[str] = {
    "AI", "US", "CEO", "CFO", "CTO", "AWS", "API", "USD",
    "FCF", "EPS", "ARR", "TCV", "FMP", "SEC", "ETF", "IPO",
    "LLM", "YOY", "QOQ", "GAAP", "SAAS", "IT", "OK", "IR",
    "M&A", "PE", "VC", "R&D", "HR", "PR", "ID",
    "TOC", "UK", "EU", "UN", "NY", "DC",
    "FY",
}


def _sanitize_bracket_tickers(text: str) -> str:
    """修复 LLM 偶发把 [TICKER] 拆成多行（如 [EL\\n]）导致括号断裂。"""
    s = text or ""
    return re.sub(
        r"\[([A-Za-z]{1,6})\s*\n+\s*\]",
        lambda m: f"[{m.group(1).strip().upper()}]",
        s,
    )


def _filter_by_ticker_whitelist(text: str, allowed_set: set[str]) -> str:
    """白名单外 ticker：整行仅 rogue 则丢弃；若同行仍含本板块标的则只涂改 rogue。"""
    _redact_placeholder = "〔非本板块标的〕"
    _allowed = {str(x).strip().upper() for x in (allowed_set or set()) if str(x).strip()}

    def _line_has_allowed(_ln: str) -> bool:
        for _sym in _allowed:
            if re.search(rf"\[{re.escape(_sym)}\]", _ln, flags=re.IGNORECASE):
                return True
            if re.search(rf"\b{re.escape(_sym)}\b", _ln, flags=re.IGNORECASE):
                return True
        return False

    def _redact_line(_ln: str, _rogue_syms: set[str]) -> str:
        out = _ln
        for _sym in sorted(_rogue_syms, key=len, reverse=True):
            out = re.sub(
                rf"\[{re.escape(_sym)}\]",
                _redact_placeholder,
                out,
                flags=re.IGNORECASE,
            )
            out = re.sub(
                rf"\b{re.escape(_sym)}\b",
                _redact_placeholder,
                out,
                flags=re.IGNORECASE,
            )
        out = re.sub(
            r"〔非本板块标的〕(?:[,、，]\s*〔非本板块标的〕)+",
            _redact_placeholder,
            out,
        )
        return out

    _filtered_lines: list[str] = []
    for _line in (text or "").split("\n"):
        bracket_tickers = {x.upper() for x in re.findall(r"\[([A-Za-z]{2,6})\]", _line)}
        word_tickers = set(re.findall(r"\b([A-Z]{2,6})\b", _line))
        _line_rogue = (bracket_tickers - _allowed) | (word_tickers - _allowed - _COMMON_ABBREVS)
        if not _line_rogue:
            _filtered_lines.append(_line)
            continue
        if _line_has_allowed(_line):
            _filtered_lines.append(_redact_line(_line, _line_rogue))
            continue
        logger.warning("Ticker白名单过滤丢弃整行(rogue=%s): %s", _line_rogue, _line.strip())
        continue
    return "\n".join(_filtered_lines)


FISCAL_YEAR_END: dict[str, str] = {
    "MDB": "Jan 31",
    "ZM": "Jan 31",
    "BAH": "Mar 31",
    "TGT": "Feb 1",
    "DG": "Jan 31",
}


def _special_structure_guidance_for_tickers(tickers: list[str]) -> str:
    """
    行业/公司级财报结构说明模板（用于约束 LLM 对口径的理解）。
    仅在命中相关公司时输出对应条目。
    """
    tset = {(t or "").strip().upper() for t in (tickers or []) if (t or "").strip()}
    notes: list[str] = []
    if "HCA" in tset:
        notes.append(
            "[医疗/医院行业 — HCA]\n"
            "支付方收入拆分（Payer Mix）仅代表净患者收入，不等于公司总收入。"
            "展示各支付方金额时，必须注明“以下为净患者收入口径，非公司合并总收入”；"
            "禁止将各支付方金额相加后与公司总收入直接比较。"
        )
    if "JLL" in tset:
        notes.append(
            "[房地产服务行业 — JLL]\n"
            "分部收入按财年披露。Step 2 与 Step 6 必须使用同一财年口径；"
            "若分部数据仅有 FY2024，需同步以 FY2024 口径展示或明确标注财年差异。"
        )
    ncf = sorted(tset & {"MDB", "ZM", "BAH"})
    if ncf:
        notes.append(
            "[非日历财年公司 — "
            + ", ".join(ncf)
            + "]\n"
            "该类公司财年截止日非 12 月 31 日。所有 FY 标注必须同时注明实际起止日期"
            "（按公司披露为准），例如：FY2026（YYYY-MM-DD 至 YYYY-MM-DD）。"
        )
    if not notes:
        return ""
    return "\n\n【财报结构说明模板（严格遵守）】\n" + "\n\n".join(notes) + "\n"


def _extract_revenue_candidates_from_earnings_text(text: str) -> list[float]:
    """
    从逐字稿段落文本中提取“管理层口述 revenue/sales”金额（美元）。
    仅提取含 revenue/sales 且包含 FY/full year 线索的句子，降低季度口径误匹配。
    """
    s = text or ""
    if not s:
        return []
    candidates: list[float] = []
    pat = re.compile(
        r"(?i)\$?\s*(\d+(?:\.\d+)?)\s*(billion|million|thousand|[BMK])\b"
    )
    for line in re.split(r"[\n\.。;；]", s):
        ll = line.lower()
        if ("revenue" not in ll and "sales" not in ll) or (
            "full year" not in ll and "fiscal year" not in ll and "fy" not in ll
        ):
            continue
        for m in pat.finditer(line):
            num = float(m.group(1))
            unit = m.group(2).lower()
            if unit in ("b", "billion"):
                mul = 1e9
            elif unit in ("m", "million"):
                mul = 1e6
            else:
                mul = 1e3
            candidates.append(num * mul)
    return candidates


def check_fiscal_year_consistency(
    ticker: str, step2_fy: int | None, step6_fy: int | None
) -> str:
    """确保 Step 2 分部数据与 Step 6 Revenue 尽量对齐同一财年。"""
    sym = (ticker or "").strip().upper()
    s2 = f"FY{step2_fy}" if isinstance(step2_fy, int) else "FY未知"
    s6 = f"FY{step6_fy}" if isinstance(step6_fy, int) else "FY未知"
    if step2_fy != step6_fy:
        return f"⚠️ {sym}: Step2 使用 {s2} 数据，Step6 使用 {s6} 数据，财年不一致"
    if sym in FISCAL_YEAR_END and isinstance(step6_fy, int):
        return (
            f"ℹ️ {sym}: 非日历财年，{s6} 实际对应截止日 {FISCAL_YEAR_END[sym]}"
        )
    return "OK"


def _validate_and_sanitize_financials(
    ticker: str, rows: list[Any]
) -> tuple[list[Any], list[str]]:
    """
    规则化校验 FMP 财务字段，并在异常时暂停自动填入（置 None）。

    规则：
    1) EBITDA 必须 >= Net Income（同年）
    2) 非软件例外公司净利率不得 > 35%
    3) 毛利率同比变化不得 > 500bps
    """
    sym = (ticker or "").strip().upper()
    if not rows:
        return [], []
    sanitized = [r.model_copy(deep=True) if hasattr(r, "model_copy") else r for r in rows]
    sanitized_sorted = sorted(
        sanitized, key=lambda r: getattr(r, "year", 0) or 0, reverse=True
    )
    issues: list[str] = []
    sec_by_year: dict[int, Any] = {}
    ec_revenue_candidates: list[float] = []
    try:
        from research_automation.extractors.sec_edgar import get_financial_statements

        for r in sanitized_sorted:
            yy = getattr(r, "year", None)
            if isinstance(yy, int) and yy not in sec_by_year:
                sec_by_year[yy] = get_financial_statements(sym, yy)
    except Exception:
        pass
    try:
        from research_automation.services.earnings_service import (
            EarningsAnalysisError,
            analyze_earnings_call,
        )

        now = datetime.now(timezone.utc)
        ec_year = now.year
        ec_quarter = (now.month - 1) // 3 + 1
        if ec_quarter == 1:
            ec_quarter = 4
            ec_year -= 1
        else:
            ec_quarter -= 1
        try:
            ec = analyze_earnings_call(sym, ec_year, ec_quarter)
            raw_text = "\n".join((getattr(ec, "source_paragraphs", None) or {}).values())
            ec_revenue_candidates = _extract_revenue_candidates_from_earnings_text(raw_text)
        except EarningsAnalysisError:
            pass
    except Exception:
        pass

    # 规则1 & 规则2：逐年检查
    for r in sanitized_sorted:
        y = getattr(r, "year", None)
        rev = getattr(r, "revenue", None)
        ni = getattr(r, "net_income", None)
        eb = getattr(r, "ebitda", None)
        # 保存清空前的原始 Revenue，供净利率校验使用
        raw_revenue = rev
        if eb is not None and ni is not None and float(eb) < float(ni):
            issues.append(
                f"{sym} FY{y}: EBITDA {float(eb):.3g} < Net Income {float(ni):.3g}"
            )
            # 暂停该字段自动填入
            setattr(r, "ebitda", None)

        # 1C-Net Income 双源核验：FMP vs SEC EDGAR XBRL
        sec_row = sec_by_year.get(int(y)) if isinstance(y, int) else None
        sec_ni = getattr(sec_row, "net_income", None) if sec_row is not None else None
        if ni is not None and sec_ni is not None and abs(float(sec_ni)) > 0:
            ni_gap = abs(float(ni) - float(sec_ni)) / abs(float(sec_ni))
            if ni_gap > 0.02:
                issues.append(
                    f"{sym} FY{y}: Net Income FMP/SEC 偏差 {ni_gap:.1%} (>2%)"
                )
                setattr(r, "net_income", None)

        # 1C-Revenue 双源核验：FMP vs 管理层电话会口述数字
        if rev is not None and ec_revenue_candidates:
            rev_f = float(rev)
            # 取最接近 FMP 年度收入的口述值，偏差>2%则告警
            nearest = min(ec_revenue_candidates, key=lambda v: abs(v - rev_f))
            if rev_f != 0:
                rev_gap = abs(nearest - rev_f) / abs(rev_f)
                if rev_gap > 0.02:
                    issues.append(
                        f"{sym} FY{y}: Revenue FMP/电话会口述偏差 {rev_gap:.1%} (>2%)"
                    )
                    setattr(r, "revenue", None)

        # 修复4：使用清空前的原始 Revenue 做净利率校验，避免被 Revenue 置空影响
        ni_after = getattr(r, "net_income", None)
        if raw_revenue is not None and float(raw_revenue) > 0 and ni_after is not None:
            net_margin = float(ni_after) / float(raw_revenue)
            if net_margin > 0.40:
                issues.append(
                    f"{sym} FY{y}: 净利率 {net_margin:.1%} 超过40%绝对阈值，net_income已清空"
                )
                setattr(r, "net_income", None)
            elif net_margin > 0.35 and sym not in SOFTWARE_EXCEPTIONS:
                issues.append(
                    f"{sym} FY{y}: 净利率 {net_margin:.1%} 超过35%阈值，net_income已清空"
                )
                setattr(r, "net_income", None)

        # Revenue 为空时，依赖 Revenue 才有意义的字段一并清空
        if getattr(r, "revenue", None) is None:
            if getattr(r, "ebitda", None) is not None:
                issues.append(f"{sym} FY{y}: Revenue已清空，EBITDA联动置空")
                setattr(r, "ebitda", None)
            if getattr(r, "gross_margin", None) is not None:
                issues.append(f"{sym} FY{y}: Revenue已清空，gross_margin联动置空")
                setattr(r, "gross_margin", None)

    # 规则3：同比毛利率变动
    for idx, cur in enumerate(sanitized_sorted[:-1]):
        prev = sanitized_sorted[idx + 1]
        gm_cur = getattr(cur, "gross_margin", None)
        gm_prev = getattr(prev, "gross_margin", None)
        if gm_cur is None or gm_prev is None:
            continue
        delta = abs(float(gm_cur) - float(gm_prev))
        if delta > 0.05:
            issues.append(
                f"{sym} FY{getattr(prev, 'year', 'N/A')}→FY{getattr(cur, 'year', 'N/A')}: "
                f"毛利率变动 {delta:.1%} 超过 500bps"
            )
            # 暂停当前年毛利率自动填入
            setattr(cur, "gross_margin", None)

    # 1C-Gross Margin 自洽核验：Revenue × GM% 与 Gross Profit 字段比对
    try:
        from research_automation.extractors.fmp_client import get_income_statement_year_fields

        for r in sanitized_sorted:
            y = getattr(r, "year", None)
            if not isinstance(y, int):
                continue
            rev = getattr(r, "revenue", None)
            gm = getattr(r, "gross_margin", None)
            if rev is None or gm is None:
                continue
            incf = get_income_statement_year_fields(sym, y)
            gp = incf.get("gross_profit")
            if gp is None:
                continue
            expected_gp = float(rev) * float(gm)
            if abs(float(gp)) > 0:
                gp_gap = abs(expected_gp - float(gp)) / abs(float(gp))
                if gp_gap > 0.02:
                    issues.append(
                        f"{sym} FY{y}: Gross Margin 反推毛利与 grossProfit 偏差 {gp_gap:.1%} (>2%)"
                    )
                    setattr(r, "gross_margin", None)
    except Exception:
        pass

    return sanitized_sorted, issues


def _get_validated_financials(ticker: str, years: int = 3) -> tuple[list[Any], list[str]]:
    """统一入口：先取 FMP 财务，再做合理性校验与字段降级。"""
    from research_automation.extractors.fmp_client import get_financials

    rows = get_financials(ticker, years=years)
    return _validate_and_sanitize_financials(ticker, rows)


_generation_locks: dict[str, threading.Lock] = {}
_generation_locks_mutex = threading.Lock()


def _get_sector_lock(sector: str) -> threading.Lock:
    with _generation_locks_mutex:
        if sector not in _generation_locks:
            _generation_locks[sector] = threading.Lock()
        return _generation_locks[sector]


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


def _parse_llm_json_payload(raw: str) -> dict[str, Any] | None:
    """尽量从 LLM 文本中提取首个 JSON 对象并解析。"""
    s = (raw or "").strip()
    if not s:
        return None

    # 先去掉常见 markdown fenced code block 包裹
    if s.startswith("```"):
        s = re.sub(r"^```(?:json)?\s*", "", s, flags=re.IGNORECASE)
        s = re.sub(r"\s*```$", "", s)
        s = s.strip()

    # 若含解释文字，抽取最外层 JSON 对象
    m = re.search(r"\{[\s\S]*\}", s)
    if m:
        s = m.group(0).strip()

    try:
        parsed = json.loads(s)
    except Exception:
        return None
    return parsed if isinstance(parsed, dict) else None


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
    from research_automation.extractors.llm_client import chat

    lines: list[str] = ["## Sector 总览", ""]

    # 收集各公司最新财务数据
    company_snapshots: list[str] = []
    for rec, _signals, _insider, _below, _had in per_company:
        t = rec.ticker
        disp = company_display_name(t, rec.company_name)
        try:
            financials, _issues = _get_validated_financials(t, years=2)
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
    from research_automation.services.earnings_service import (
        EarningsAnalysisError,
        analyze_earnings_call,
    )
    from research_automation.services.profile_service import (
        ProfileGenerationError,
        get_profile,
    )

    lines: list[str] = ["## 个股快速扫描（最新财年）", ""]
    lines.append("| Ticker | Revenue | YoY | Gross Margin | EBITDA | Net Debt/Eq | 本周关键信号 |")
    lines.append("|--------|---------|-----|--------------|--------|-------------|------------|")

    now = datetime.now(timezone.utc)
    ec_year = now.year
    ec_quarter = (now.month - 1) // 3 + 1
    if ec_quarter == 1:
        ec_quarter = 4
        ec_year -= 1
    else:
        ec_quarter -= 1

    for rec, signals, _insider, _below, _had in per_company:
        t = rec.ticker
        try:
            financials, fin_issues = _get_validated_financials(t, years=2)
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

            # 分析覆盖缺失标记：Step3(10-K画像) + Step4(Earning Call) 均不可用
            profile_ok = True
            earnings_ok = True
            try:
                get_profile(t)
            except (ProfileGenerationError, Exception):
                profile_ok = False
            try:
                analyze_earnings_call(t, ec_year, ec_quarter)
            except (EarningsAnalysisError, Exception):
                earnings_ok = False
            if not profile_ok and not earnings_ok:
                top_signal = "⚠️ 逐字稿+10K均不可用，分析覆盖严重缺失"
            if fin_issues:
                top_signal = (
                    (top_signal + "；") if top_signal else ""
                ) + "⚠️ 财务字段异常，部分指标已暂停自动填入"

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


def _get_revenue_segments(
    ticker: str, prefer_year: int | None = None
) -> tuple[list[dict[str, Any]], int | None, str]:
    """
    统一分部数据入口（供简介与 Step2 共用）。
    返回：(rows, fiscal_year, source_label)。
    """
    from research_automation.extractors.fmp_client import get_segment_revenue
    from research_automation.services.profile_service import (
        ProfileGenerationError,
        get_profile,
    )

    if prefer_year is None:
        prefer_year = datetime.now(timezone.utc).year
    years = [prefer_year, prefer_year - 1, prefer_year - 2, prefer_year - 3]
    for y in years:
        rows = get_segment_revenue(ticker, y)
        if rows:
            return rows, y, "FMP Revenue Segmentation"

    try:
        p = get_profile(ticker)
    except (ProfileGenerationError, Exception):
        return [], None, "NONE"
    raw = p.revenue_by_segment or []
    out_rows: list[dict[str, Any]] = []
    for seg in raw:
        if isinstance(seg, dict):
            name = str(seg.get("segment_name") or seg.get("segment") or "").strip()
            pct_raw = seg.get("percentage")
            abs_raw = seg.get("absolute")
        else:
            name = str(getattr(seg, "segment_name", "") or "").strip()
            pct_raw = getattr(seg, "percentage", None)
            abs_raw = getattr(seg, "absolute", None)
        if not name:
            continue
        pct: float | None = None
        if pct_raw is not None:
            try:
                if isinstance(pct_raw, str):
                    pct = float(pct_raw.replace("%", "").strip())
                else:
                    pct = float(pct_raw)
            except (TypeError, ValueError):
                pct = None
        abs_val: float | None = None
        if abs_raw is not None:
            try:
                abs_val = float(abs_raw)
            except (TypeError, ValueError):
                abs_val = None
        row: dict[str, Any] = {"segment": name}
        if pct is not None:
            row["percentage"] = pct
        if abs_val is not None:
            row["absolute"] = abs_val
        out_rows.append(row)
    inferred_year = datetime.now(timezone.utc).year - 1 if out_rows else None
    if out_rows:
        return out_rows, inferred_year, "SEC 10-K文本提取"
    return [], None, "NONE"


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
    from research_automation.extractors.fmp_client import get_geographic_revenue
    lines: list[str] = ["## Step 2｜业务占比（产品线 + 地理收入）", ""]

    # ── Sector 级别业务占比总结（纯数据，无LLM）──────────────────

    def _sum_segment_abs(rows: list[Any]) -> float:
        total_abs = 0.0
        for seg in rows or []:
            if isinstance(seg, dict):
                raw = seg.get("absolute")
            else:
                raw = getattr(seg, "absolute", None)
            if raw is None:
                continue
            try:
                total_abs += float(raw)
            except (TypeError, ValueError):
                continue
        return total_abs

    def _infer_year_from_total(ticker: str, seg_total: float) -> int | None:
        if seg_total <= 0:
            return None
        try:
            fin_rows, _issues = _get_validated_financials(ticker, years=4)
        except Exception:
            return None
        best_year: int | None = None
        best_gap = float("inf")
        for fr in fin_rows or []:
            y = getattr(fr, "year", None)
            rev = getattr(fr, "revenue", None)
            if y is None or rev is None:
                continue
            try:
                y_int = int(y)
                rev_f = float(rev)
            except (TypeError, ValueError):
                continue
            if rev_f <= 0:
                continue
            gap = abs(seg_total - rev_f) / max(seg_total, rev_f)
            if gap < best_gap:
                best_gap = gap
                best_year = y_int
        return best_year

    def _get_verified_total_revenue(ticker: str, fiscal_year: int | None) -> float | None:
        if fiscal_year is None:
            return None
        try:
            fin_rows, _issues = _get_validated_financials(ticker, years=6)
        except Exception:
            return None
        for fr in fin_rows or []:
            y = getattr(fr, "year", None)
            rev = getattr(fr, "revenue", None)
            if y == fiscal_year and rev is not None:
                try:
                    rv = float(rev)
                except (TypeError, ValueError):
                    return None
                return rv if rv > 0 else None
        return None

    def calculate_segment_dollars(
        segments_pct: dict[str, float], total_revenue: float
    ) -> dict[str, dict[str, float]]:
        """
        强制用同一个 total_revenue 基数计算分部金额。
        total_revenue 必须来自 Step 6 已验证 revenue，不允许 AI 自行选择。
        """
        result: dict[str, dict[str, float]] = {}
        total_check = 0.0
        for segment, pct in segments_pct.items():
            dollar = float(total_revenue) * float(pct)
            result[segment] = {"pct": float(pct), "dollar": dollar}
            total_check += float(pct)
        if not (0.98 <= total_check <= 1.02):
            raise ValueError(f"分部占比合计 {total_check:.1%}，不等于100%，数据有误")
        return result

    def check_segment_sum(
        ticker: str,
        segments: dict[str, dict[str, float]],
        step6_revenue: float,
        tolerance: float = 0.02,
    ) -> dict[str, Any]:
        seg_total = sum(float(s.get("dollar") or 0.0) for s in segments.values())
        base = float(step6_revenue)
        if base <= 0:
            return {
                "status": "ERROR",
                "message": f"{ticker} Step6 Revenue 无效（<=0），无法校验分部合计",
            }
        diff_pct = abs(seg_total - base) / base
        if diff_pct > tolerance:
            return {
                "status": "ERROR",
                "message": (
                    f"{ticker} 分部合计 ${seg_total/1e9:.2f}B ≠ "
                    f"总收入 ${base/1e9:.2f}B，差距 {diff_pct:.1%}，请检查基数"
                ),
            }
        return {"status": "OK"}

    def _system_enforce_segment_dollars(
        ticker: str, rows: list[dict[str, Any]], fiscal_year: int | None
    ) -> tuple[list[dict[str, Any]], str | None]:
        """
        用 Step6 已验证 Revenue 回填/重算分部 absolute，避免模型或上游自行选错基数。
        返回 (new_rows, warn_text)。
        """
        if not rows:
            return rows, None
        base_rev = _get_verified_total_revenue(ticker, fiscal_year)
        if base_rev is None:
            return rows, "⚠️ Step6 已验证 Revenue 不可用，未执行分部金额系统重算"
        pct_map: dict[str, float] = {}
        for r in rows:
            nm = str(r.get("segment") or "").strip()
            p = r.get("percentage")
            if not nm or p is None:
                continue
            try:
                pct_map[nm] = float(p) / 100.0
            except (TypeError, ValueError):
                continue
        if not pct_map:
            return rows, "⚠️ 分部占比缺失，未执行分部金额系统重算"
        try:
            calced = calculate_segment_dollars(pct_map, base_rev)
        except ValueError as e:
            return rows, f"⚠️ {e}，已暂停分部金额自动填入"
        sum_check = check_segment_sum(ticker, calced, base_rev, tolerance=0.02)
        if sum_check.get("status") != "OK":
            out_rows = []
            for r in rows:
                rr = dict(r)
                rr["absolute"] = None
                rr["calc_basis"] = "step6_verified_revenue_sum_check_failed"
                rr["sum_check_failed"] = True
                out_rows.append(rr)
            return out_rows, f"🔴 **{sum_check.get('message')}**（金额列已隐藏，仅保留占比）"
        out_rows: list[dict[str, Any]] = []
        for r in rows:
            rr = dict(r)
            nm = str(rr.get("segment") or "").strip()
            if nm in calced:
                rr["absolute"] = calced[nm]["dollar"]
                rr["calc_basis"] = "step6_verified_revenue"
            out_rows.append(rr)
        return out_rows, None

    try:
        # 收集各公司分部数据
        company_segment_data: dict[str, list] = {}
        covered = 0
        no_data_tickers: list[str] = []

        for rec, *_ in per_company:
            t = rec.ticker
            data, _seg_year, _seg_source = _get_revenue_segments(t)
            if data:
                enforced, _warn = _system_enforce_segment_dollars(t, data, _seg_year)
                company_segment_data[t] = enforced
                covered += 1
            else:
                no_data_tickers.append(t)

        if covered > 0:
            # 构建传给 LLM 的数据
            from research_automation.extractors.llm_client import chat as _chat2

            seg_json = json.dumps(company_segment_data, ensure_ascii=False, indent=2)
            sector_name = per_company[0][0].sector if per_company else "未知板块"
            struct_notes = _special_structure_guidance_for_tickers(
                [rec.ticker for rec, *_ in per_company]
            )

            prompt = f"""你是金融数据分析师。以下是 {sector_name} 板块各公司的分部收入数据（来源：FMP）。

请完成两个任务，返回严格的 JSON，不要任何额外文字、不要 markdown 代码块：

{{
  "business_type_distribution": [
    {{"type": "咨询与数字化转型", "companies": ["ACN", "CTSH"], "description": "为企业提供IT咨询、数字化转型服务"}},
    {{"type": "企业软件与SaaS", "companies": ["IBM", "ZM"], "description": "软件产品、云服务、数据库"}}
  ],
  "unified_segments": [
    {{"category": "Consulting & Services", "total_absolute": 123456789, "companies": ["ACN", "CTSH"]}},
    {{"category": "Software & Cloud", "total_absolute": 987654321, "companies": ["IBM", "ZM"]}}
  ]
}}

{struct_notes}

各公司分部数据：
{seg_json}

要求：
1. business_type_distribution 按主营业务归类，每家公司只归入一个类型，不超过6个类型
2. unified_segments 的归类必须与该板块（{sector_name}）的核心业务相关，禁止把某家公司的非核心segment（如零售商的食品收入、制造商的金融服务收入）单独列为板块级类别
3. 如果某家公司的某个segment与板块主题明显不相关（如AI板块中出现食品、零售等），应将其归入该公司最接近板块主题的类别，或标注为"其他"
4. 每家公司只能归入一个business_type，unified_segments合并同类绝对金额之和
5. 只基于提供的数据，不推断或补充数据中没有的信息
6. 所有金额单位与原始数据一致（美元）
7. 禁止凭空捏造segment数据，只能使用上面提供的原始数据
8. 计算各分部美元金额时，必须且只能使用本公司在 Step 6 Revenue 表格中已确认的总收入数字作为基数；禁止使用任何其他来源的收入数字

在输出地理收入表格后，检查所有地理分部名称：
如果所有分部名称中均不包含任何可识别的地理信息
（国家名、洲名、地区名，如 United States、China、Europe、Asia、
Americas、EMEA、Pacific、Latin、International、North、South 等），
则说明这是公司内部管理架构命名而非地理收入，
在表格正下方紧接一行注释：
「⚠️ 注：以上分部名称不含地理信息，可能为公司内部运营架构划分，
非跨国地理收入分布，请结合公司年报核实。」
若任意一个分部名称中含有可识别的地理信息，则正常输出，不加任何注释。
"""

            llm_result = None
            try:
                raw = _chat2(prompt, max_tokens=1200, response_format={"type": "json_object"})
                llm_result = _parse_llm_json_payload(raw)
                if llm_result is None:
                    logger.warning(
                        "Step2 LLM 未返回可解析 JSON，fallback。raw_preview=%s",
                        (raw or "")[:240].replace("\n", " "),
                    )
            except Exception as e:
                logger.warning("Step2 LLM 归类失败，fallback 到简单加总: %s", e)

            lines.append(
                f"> **数据来源**：FMP Revenue Segmentation | **覆盖**：{covered}/{len(per_company)} 家公司 | "
                f"**暂无数据**：{', '.join(no_data_tickers) if no_data_tickers else '无'}"
            )
            lines.append("")

            if llm_result:
                # A. 板块业务类型分布
                biz_dist = llm_result.get("business_type_distribution", [])
                if biz_dist:
                    lines.append("**板块业务类型分布：**")
                    lines.append("")
                    lines.append("| 业务类型 | 涉及公司 | 说明 |")
                    lines.append("|---------|---------|------|")
                    for item in biz_dist:
                        btype = item.get("type", "")
                        comps = "、".join(item.get("companies", []))
                        desc = item.get("description", "")
                        lines.append(f"| {btype} | {comps} | {desc} |")
                    lines.append("")

                # B. 统一分类后的收入结构
                unified = llm_result.get("unified_segments", [])
                if unified:
                    total_unified = sum(u.get("total_absolute", 0) for u in unified)
                    unified_sorted = sorted(
                        unified, key=lambda x: x.get("total_absolute", 0), reverse=True
                    )
                    lines.append("**板块收入结构（LLM归类后）：**")
                    lines.append("")
                    for u in unified_sorted:
                        cat = u.get("category", "")
                        amt = u.get("total_absolute", 0)
                        comps = "、".join(u.get("companies", []))
                        pct = amt / total_unified * 100 if total_unified > 0 else 0
                        lines.append(f"- {cat}：{pct:.1f}%（${amt/1e9:.1f}B）—— {comps}")
                    lines.append("")

                lines.append("> ⚠️ 系统辅助归类，基于FMP分部数据由LLM映射，请核对原始数据。")
                lines.append("")
            else:
                # fallback：简单列出各公司主要分部
                lines.append("**各公司主要业务线（FMP数据）：**")
                lines.append("")
                for t, data in company_segment_data.items():
                    top = data[0] if data else None
                    if top:
                        lines.append(f"- {t}：{top['segment']}（{top['percentage']:.1f}%）等")
                lines.append("")
        else:
            lines.append(
                f"> **数据来源**：FMP Revenue Segmentation | **覆盖**：0/{len(per_company)} 家公司 | "
                f"**暂无数据**：{', '.join(no_data_tickers) if no_data_tickers else '无'}"
            )
            lines.append("")

        lines.append("<!--- COMPANY_DETAILS_START --->")
        lines.append("")
    except Exception as e:
        logger.warning("Step2 聚合异常，降级到空聚合: %s", e)
        lines.append(
            f"> **数据来源**：FMP Revenue Segmentation | **覆盖**：0/{len(per_company)} 家公司 | **暂无数据**：异常"
        )
        lines.append("")
        lines.append("**各公司主要业务线（FMP数据）：**")
        lines.append("")
        lines.append("<!--- COMPANY_DETAILS_START --->")
        lines.append("")
    # ── Sector 总结 END ──────────────────────────────────────────

    for rec, _signals, _insider, _below, _had in per_company:
        t = rec.ticker
        disp = company_display_name(t, rec.company_name)
        lines.append(f"### {t} — {disp}")
        lines.append("")
        data, year_used, seg_source = _get_revenue_segments(t)
        if not data:
            lines.append("**收入结构（产品线）：** *暂无可用分部数据（FMP未收录，10-K提取失败）*")
            lines.append("")
            continue
        step6_rows, _step6_issues = _get_validated_financials(t, years=1)
        step6_fy = None
        if step6_rows:
            try:
                step6_fy = int(getattr(step6_rows[0], "year", 0) or 0) or None
            except (TypeError, ValueError):
                step6_fy = None
        fy_check_msg = check_fiscal_year_consistency(t, year_used, step6_fy)
        data, calc_warn = _system_enforce_segment_dollars(t, data, year_used)
        total = 0.0
        has_abs = False
        for d in data:
            v = d.get("absolute")
            if v is None:
                continue
            try:
                total += float(v)
                has_abs = True
            except (TypeError, ValueError):
                continue
        year_label = f"{year_used}" if year_used is not None else "N/A"
        total_disp = f"${total/1e9:.2f}B" if has_abs else "—"
        lines.append(f"**财年{year_label} revenue breakdown**（总计 {total_disp}）：")
        lines.append("")
        lines.append("| 业务线 | 占比 | 收入 |")
        lines.append("|--------|------|------|")
        for seg in data:
            pct_raw = seg.get("percentage")
            if pct_raw is None:
                pct_disp = "—"
            else:
                try:
                    pct_disp = f"{float(pct_raw):.1f}%"
                except (TypeError, ValueError):
                    pct_disp = "—"
            abs_raw = seg.get("absolute")
            if abs_raw is None:
                abs_disp = "—"
            else:
                try:
                    abs_disp = f"${float(abs_raw)/1e9:.2f}B"
                except (TypeError, ValueError):
                    abs_disp = "—"
            lines.append(f"| {seg['segment']} | {pct_disp} | {abs_disp} |")
        lines.append("")
        if calc_warn:
            lines.append(calc_warn)
            lines.append("")
        if fy_check_msg != "OK":
            lines.append(fy_check_msg)
            lines.append("")
        if year_used is not None and seg_source != "NONE":
            lines.append(f"*（数据来源：{seg_source}，FY{year_used}）*")
            lines.append("")

        # 跨Step一致性校验：简介 vs Step2 分部总额差异
        try:
            intro_rows, intro_year, _intro_source = _get_revenue_segments(t)
            intro_total = _sum_segment_abs(intro_rows)
            if intro_total > 0 and has_abs:
                if intro_year is None:
                    intro_year = _infer_year_from_total(t, intro_total)
                if (
                    intro_year is not None
                    and year_used is not None
                    and intro_year != year_used
                ):
                    diff_ratio = abs(intro_total - total) / max(intro_total, total)
                    if diff_ratio > 0.05:
                        lines.append(
                            f"⚠️ 注意：简介与Step 2分部数据来自不同财年（简介：FY{intro_year}，Step 2：FY{year_used}），请以Step 6财务表为准。"
                        )
                        lines.append("")
        except Exception:
            pass

        # 地理收入拆分（FMP geographic；与产品线高度重合或明显业务线用词时不展示）
        geo_data = get_geographic_revenue(t, year_used) if year_used is not None else None
        if geo_data:
            geo_total = sum(g.get('absolute', 0) or 0 for g in geo_data)
            verified_rev = _get_verified_total_revenue(t, year_used)
            geo_coverage = geo_total / verified_rev if verified_rev and verified_rev > 0 else None
            if geo_coverage is not None and geo_coverage < 0.85:
                lines.append(
                    f"**地理收入拆分（FY{year_label}）：** "
                    f"*暂无公司级地理收入数据（FMP返回数据仅覆盖总收入的{geo_coverage:.0%}，疑为分部级数据，已过滤）*"
                )
                lines.append("")
            else:
                lines.append(f"**地理收入拆分（FY{year_label}）：**")
                lines.append("")
                lines.append("| 地区 | 占比 | 收入 |")
                lines.append("|------|------|------|")
                for g in geo_data:
                    lines.append(
                        f"| {g['region']} | {g['percentage']:.1f}% | "
                        f"${g['absolute']/1e9:.2f}B |"
                    )
                lines.append("")
        elif year_used is not None:
            lines.append(
                f"**地理收入拆分（FY{year_label}）：** "
                "*暂无地理收入拆分数据（FMP 无可用地理分部或与产品线拆分重合已过滤）*"
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
            profile_field_sources = getattr(profile, "field_sources", None) or {}
            fg_sources = profile_field_sources.get("future_guidance", [])
            if fg and fg not in ("原文未明确提及", "NOT_FOUND"):
                chunk.extend(
                    [
                        "**未来展望与指引：**",
                        "",
                        fg,
                        "",
                    ]
                )
                if fg_sources:
                    chunk.append(f"*来源：{', '.join(fg_sources)}*")
                    chunk.append("")
            else:
                chunk.extend(["**未来展望与指引：** *原文未明确提及*", ""])
            iv = (profile.industry_view or "").strip()
            # 过滤 10-K Item 1A 风险因素模板句
            # 这类句子对投研分析价值极低，不应进入 Sector 总结
            _RISK_FACTOR_PATTERNS = (
                "may have a material adverse effect",
                "could have a material adverse",
                "there can be no assurance",
                "we cannot predict",
                "subject to various risks",
                "forward-looking statements",
                "actual results may differ",
                "risks and uncertainties",
            )
            _iv_lower = iv.lower()
            _is_risk_template = any(p in _iv_lower for p in _RISK_FACTOR_PATTERNS)
            if _is_risk_template:
                logger.debug(
                    "Step3: industry_view 含 Risk Factors 模板句，跳过 ticker=%s", t
                )
                iv = ""
            if iv and iv not in ("原文未明确提及", "NOT_FOUND"):
                chunk.extend(
                    [
                        "**行业判断（管理层视角）：**",
                        "",
                        iv,
                    ]
                )
                if profile.industry_view_source:
                    chunk.append(f"*来源：{profile.industry_view_source}*")
                else:
                    # 有内容但无来源，不输出行业判断
                    # 回退：移除已加入的 industry_view 内容
                    while chunk and chunk[-1] != "**行业判断（管理层视角）：**":
                        chunk.pop()
                    if chunk:
                        chunk.pop()  # 移除标题本身
                    chunk.extend(["**行业判断（管理层视角）：** *无可溯源原文*", ""])
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
    from research_automation.extractors.llm_client import chat_with_retry as _chat3

    # 收集各公司 future_guidance 和 industry_view
    outlook_briefs: list[str] = []
    for rec, *_ in per_company:
        blk = results_map.get(rec.ticker, [])
        if not blk:
            continue
        blk_text = '\n'.join(blk)
        if '原文未明确提及' in blk_text and 'NOT_FOUND' in blk_text:
            continue
        # 控制单家输入长度，避免 prompt 爆炸；过低会截断关税/指引等长段落（如 PPG）
        outlook_briefs.append(f"【{rec.ticker}】\n{blk_text[:1100]}")

    if len(outlook_briefs) >= 2:
        briefs_text = '\n\n'.join(outlook_briefs)
        all_tickers_in_sector = [rec.ticker for rec, *_ in per_company]
        _s3_guard = (
            "严格要求：只输出本板块内容。所有结论必须有管理层原话或具体数字依据。"
            "禁止推断、禁止投资建议、禁止输出额外板块或总结段落。\n"
            "输出格式要求：先2-4句连贯段落总结，再按主题或公司分组列支撑数据（小标题+bullet）；"
            "每条必须标注来源公司和具体数字/原话。"
        )
        _s3_common = f"""以下是该板块各公司管理层对未来的展望与行业判断：

{briefs_text}

本板块共 {len(all_tickers_in_sector)} 家公司：{', '.join(all_tickers_in_sector)}
以上数据覆盖其中 {len(outlook_briefs)} 家。"""

        def _run_s3_block(_name: str, _task_rule: str) -> str:
            _prompt = (
                f"你是资深行业研究分析师。\n{_s3_guard}\n\n{_s3_common}\n\n{_task_rule}"
            )
            _txt = _chat3(_prompt, max_tokens=1000, timeout=300.0)
            _round = 0
            while _round < 3 and (
                _is_truncated_llm_output(_txt) or len((_txt or "").strip()) < 60
            ):
                logger.warning(
                    "Step3[%s]疑似截断，尝试续写 round=%s",
                    _name,
                    _round,
                )
                try:
                    _cont = _chat3(
                        f"以下是未完成的「{_name}」内容，请从中断处继续，只输出续写内容，不要重复已有内容：\n\n{_txt}",
                        max_tokens=1000,
                        timeout=300.0,
                    )
                    _txt = _txt.rstrip() + "\n" + _cont
                except Exception:
                    logger.warning("Step3[%s]续写失败，使用已有内容", _name)
                    break
                _round += 1
            return (_txt or "").strip()

        try:
            s3_industry = _run_s3_block(
                "行业判断",
                "第1次——【行业判断】\n基于以下各公司10-K及Earning Call内容，只提取跨2家以上公司共同出现的行业层面判断。"
                "每条必须有管理层原文支撑，末尾标注（来源：公司A、公司B）。"
                "每条不超过100字。按统一格式输出（先2-4句段落总结，再分组小标题+bullet支撑数据），禁止任何推断、点评或额外内容。",
            )
            s3_strategy = _run_s3_block(
                "战略方向",
                "第2次——【战略方向】\n基于以下各公司10-K及Earning Call内容，只提取跨2家以上公司共同出现的战略举措或方向。"
                "每条必须有管理层原文支撑，末尾标注（来源：公司A、公司B）。"
                "每条不超过100字。按统一格式输出（先2-4句段落总结，再分组小标题+bullet支撑数据），禁止任何推断、点评或额外内容。",
            )
            sector_outlook = (
                "【行业判断】\n"
                f"{s3_industry or '- （无可提取内容）'}\n\n"
                "【战略方向】\n"
                f"{s3_strategy or '- （无可提取内容）'}"
            )
            total_companies = len(per_company)
            covered_companies = len(outlook_briefs)
            covered_tickers = []
            outlook_idx = 0
            for rec, *_ in per_company:
                blk = results_map.get(rec.ticker, [])
                blk_text = '\n'.join(blk)
                has_content = (
                    ("未来展望" in blk_text and "原文未明确提及" not in blk_text[:100])
                    or ("行业判断" in blk_text and "原文未明确提及" not in blk_text[:200])
                )
                if has_content:
                    covered_tickers.append(rec.ticker)
            lines.append(
                f"> **数据来源**：各公司 10-K 及 Earning Call（SEC EDGAR / FMP）"
                f"｜**覆盖公司**：{covered_companies}/{total_companies} 家"
                f"（{', '.join(covered_tickers) if covered_tickers else '无'}）"
                f"｜**评判标准**：至少2家公司共同提及的战略方向或行业判断"
            )
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
        except Exception as e:
            logger.exception("Step3 sector总结失败，错误详情：%s", e)
            if _strict_expose_llm_errors():
                raise
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
    from .post_generation_checker import (
        build_baseline_from_rows,
        run_post_generation_check,
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

        # LLM 限流保护：最多同时 2 个并发
        MAX_WORKERS = 2  # 降低并发，减少LLM限流导致的偶发JSON解析失败

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
        baseline: dict[str, Any] = {}
        for ticker in tickers_in_order:
            try:
                rows, _issues = _get_validated_financials(ticker, years=2)
                baseline.update(build_baseline_from_rows(ticker, rows))
            except Exception:
                logger.exception("Step4 baseline构建失败 ticker=%s", ticker)

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
            from research_automation.extractors.llm_client import chat_with_retry as _chat

            # 抽取每家公司的概括和关键观点（控制token）
            company_briefs: list[str] = []
            for t, analysis in successful_analyses:
                brief_parts = [f"【{t}】"]
                if analysis.summary:
                    brief_parts.append(f"概括：{analysis.summary[:480]}")
                if analysis.management_viewpoints:
                    vp_texts = [vp.text for vp in analysis.management_viewpoints[:5]]
                    brief_parts.append(f"核心观点：{'；'.join(vp_texts)}")
                if analysis.quotations:
                    q = analysis.quotations[0]
                    brief_parts.append(f"关键原话：{q.speaker}：\"{q.quote[:220]}\"")
                if analysis.new_business_highlights:
                    nb_texts = [nb.text for nb in analysis.new_business_highlights[:5]]
                    brief_parts.append(f"新业务/亮点：{'；'.join(nb_texts)}")
                company_briefs.append('\n'.join(brief_parts))

            watch_str = '、'.join(sector_watch_items) if sector_watch_items else '无'
            briefs_text = '\n\n'.join(company_briefs)

            all_tickers_s4 = [rec.ticker for rec, *_ in per_company]
            _s4_guard = (
                "严格要求：只输出本板块内容。所有结论必须有管理层原话或具体数字依据。"
                "禁止推断、禁止投资建议、禁止输出额外板块或总结段落。\n"
                "输出格式要求：先2-4句连贯段落总结，再按主题或公司分组列支撑数据（小标题+bullet）；"
                "每条必须标注来源公司和具体数字/原话。"
            )
            _s4_common = f"""以下是{_sector}板块本季度各公司Earning Call的关键内容：

{briefs_text}

本板块共 {len(all_tickers_s4)} 家公司：{', '.join(all_tickers_s4)}
以上数据覆盖其中 {len(successful_analyses)} 家（{', '.join([t for t, _ in successful_analyses])}）。

本sector重点关注项：{watch_str}
"""

            def _run_s4_block(_name: str, _task_rule: str, _max_tokens: int) -> str:
                _prompt = (
                    f"你是资深行业研究分析师。\n{_s4_guard}\n\n{_s4_common}\n{_task_rule}"
                )
                _plen = len(_prompt)
                logger.info(
                    "Step4[%s] prompt长度：%s 字符，约 %s tokens（粗估 chars/4）",
                    _name,
                    _plen,
                    _plen // 4,
                )
                _txt = _chat(_prompt, max_tokens=_max_tokens, timeout=300.0)
                _round = 0
                while _round < 3 and (
                    _is_truncated_llm_output(_txt) or len((_txt or "").strip()) < 60
                ):
                    logger.warning(
                        "Step4[%s]疑似截断，尝试续写 round=%s",
                        _name,
                        _round,
                    )
                    try:
                        _cont = _chat(
                            f"以下是未完成的「{_name}」内容，请从中断处继续，只输出续写内容，不要重复已有内容：\n\n{_txt}",
                            max_tokens=_max_tokens,
                            timeout=300.0,
                        )
                        _txt = _txt.rstrip() + "\n" + _cont
                    except Exception:
                        logger.warning("Step4[%s]续写失败，使用已有内容", _name)
                        break
                    _round += 1
                return (_txt or "").strip()

            try:
                s4_ai = _run_s4_block(
                    "AI部署与替代进展",
                    "第1次——【AI部署与替代进展】\n只列出有具体数字或管理层原话支撑的AI部署事实。"
                    "每条末尾标注（来源：公司名+数字）。按统一格式输出（先2-4句段落总结，再分组小标题+bullet支撑数据），禁止推断和点评。",
                    1000,
                )
                s4_people = _run_s4_block(
                    "人员与组织变动",
                    "第2次——【人员与组织变动】\n只列出管理层明确披露的员工数量变化、重组计划或裁员数据。"
                    "每条末尾标注（来源：公司名+具体数字）。"
                    "对于累计费用或累计裁员数字，必须同时标注截止日期（如「截至2025年9月30日」），"
                    "若摘录中未明确截止日期则不得引用该累计数字。"
                    "若同一公司在不同时间点有多个累计数字，只引用最新一个。"
                    "按统一格式输出（先2-4句段落总结，再分组小标题+bullet支撑数据），禁止推断和点评。",
                    800,
                )
                s4_fin = _run_s4_block(
                    "营收与盈利关键数据",
                    "第3次——【营收与盈利关键数据】\n只列出各公司本季核心财务数字，每条末尾标注（来源：公司名+财年/季度）。"
                    "按统一格式输出（先2-4句段落总结，再分组小标题+bullet支撑数据），禁止任何评级或预测。",
                    800,
                )
                sector_summary = (
                    "【AI部署与替代进展】\n"
                    f"{s4_ai or '- （无可提取内容）'}\n\n"
                    "【人员与组织变动】\n"
                    f"{s4_people or '- （无可提取内容）'}\n\n"
                    "【营收与盈利关键数据】\n"
                    f"{s4_fin or '- （无可提取内容）'}"
                )
                _allowed_set_s4 = {rec.ticker.upper() for rec, *_ in per_company}
                sector_summary = _sanitize_bracket_tickers(sector_summary)
                sector_summary = _filter_by_ticker_whitelist(
                    sector_summary, _allowed_set_s4
                )
                total_companies_s4 = len(per_company) if per_company else 0
                covered_companies_s4 = len(successful_analyses)
                covered_tickers_s4 = [t for t, _ in successful_analyses]
                lines.append(
                    f"> **数据来源**：各公司 Earning Call 逐字稿（FMP / SEC EDGAR）"
                    f"｜**覆盖公司**：{covered_companies_s4}/{total_companies_s4} 家"
                    f"（{', '.join(covered_tickers_s4) if covered_tickers_s4 else '无'}）"
                    f"｜**评判标准**：跨3家以上公司出现的共同表述，每条结论附具体数字"
                )
                lines.append("> ⚠️ **以下为系统辅助总结，非原文直接提取，仅供参考。原文 quotations 请展开各公司详情查看。**")
                lines.append("")
                try:
                    checker_result = run_post_generation_check(
                        texts={"step4_sector_summary": sector_summary},
                        baseline=baseline,
                    )
                    if checker_result["summary"]["error"] > 0:
                        logger.info(
                            "[验证层C] 发现 %s 处疑似错误，见 findings",
                            checker_result["summary"]["error"],
                        )
                except Exception:
                    logger.exception("Step4后置数字核查失败，已跳过")
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
            except Exception as e:
                logger.exception("Step4 sector总结失败，错误详情：%s", e)
                if _strict_expose_llm_errors():
                    raise
        # ── Sector 级别总结 END ──────────────────────────────────────

        # 数据时效提示：员工人数、门店数等运营指标因来源时间不同可能存在差异
        lines.append(
            "> 📌 **数据时效提示**：以下各公司员工人数、门店数量等运营指标，"
            "10-K 来源截至上一财年末，Earning Call 来源截至该季度末，两者时间节点不同，"
            "如有出入请以最新季报为准。"
        )
        lines.append("")

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
                if getattr(outcome, "tag", "") == "NO_TRANSCRIPT":
                    lines.append("> ⚠️ **逐字稿暂不可用**")
                    lines.append("> 已尝试数据源：FMP / SEC EDGAR 8-K / sec-api.io")
                    lines.append(f"> 建议：确认该公司是否已发布 {year}Q{quarter} 财报电话会，或检查 API 密钥配置。")
                else:
                    lines.append("> ⚠️ **逐字稿解析失败**")
                    lines.append("> 已获取逐字稿，但 LLM 输出无法解析，请稍后重试。")
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
    *,
    sector_summary_only: bool = False,
) -> list[str]:
    lines: list[str] = ["## Step 5｜新业务 / 收购 / Insider 异动", ""]

    # ── Sector 级别新业务/收购总结（LLM）────────────────────────
    from research_automation.extractors.llm_client import chat_with_retry as _chat5
    from research_automation.services.earnings_service import (
        EarningsAnalysisError,
        analyze_earnings_call,
    )

    def _infer_latest_quarter() -> tuple[int, int]:
        now = datetime.now(timezone.utc)
        y = now.year
        q = (now.month - 1) // 3 + 1
        if q == 1:
            return y - 1, 4
        return y, q - 1

    def _has_step5_keyword(text: str) -> bool:
        tl = (text or "").lower()
        keys = (
            "acquisition",
            "acquire",
            "merger",
            "m&a",
            "strategic partnership",
            "partnership",
            "contract",
            "award",
            "venture",
            "investment",
            "capital commitment",
            "收购",
            "并购",
            "战略合作",
            "合同",
            "中标",
            "投资",
        )
        return any(k in tl for k in keys)

    def _extract_max_usd(text: str) -> float | None:
        # 识别 $2B / $315M / $200 million 这类金额，返回美元数
        import re as _re

        s = text or ""
        vals: list[float] = []
        for m in _re.finditer(r"\$?\s*(\d+(?:\.\d+)?)\s*([BbMmKk])\b", s):
            num = float(m.group(1))
            unit = m.group(2).lower()
            mul = 1e9 if unit == "b" else 1e6 if unit == "m" else 1e3
            vals.append(num * mul)
        for m in _re.finditer(
            r"\$?\s*(\d+(?:\.\d+)?)\s*(billion|million|thousand)\b", s, _re.I
        ):
            num = float(m.group(1))
            u = m.group(2).lower()
            mul = 1e9 if u == "billion" else 1e6 if u == "million" else 1e3
            vals.append(num * mul)
        return max(vals) if vals else None

    def _earning_call_step5_items(ticker: str) -> list[str]:
        y, q = _infer_latest_quarter()
        try:
            analysis = analyze_earnings_call(ticker, y, q)
        except EarningsAnalysisError:
            return []
        except Exception:
            return []
        out: list[str] = []
        for nb in getattr(analysis, "new_business_highlights", []) or []:
            txt = str(getattr(nb, "text", "") or "").strip()
            if not txt:
                continue
            amount = _extract_max_usd(txt)
            is_big_commitment = amount is not None and amount >= 1e8
            if _has_step5_keyword(txt) or is_big_commitment:
                out.append(
                    f"[{ticker}] {txt}（来源：FY{y}Q{q} Earning Call逐字稿）"
                )
        return out

    # 收集所有公司的业务信号
    all_signals_brief: list[str] = []
    coverage_tickers: list[str] = []
    no_coverage_tickers: list[str] = []
    ec_step5_items_by_ticker: dict[str, list[str]] = {}
    for rec, signals, insider, _below, _had in per_company:
        t = rec.ticker
        biz_signals = [
            s for s in signals
            if str(s.get("signal_type") or "") in ("business_change", "insider_trade")
        ]
        ec_items = _earning_call_step5_items(t)
        if ec_items:
            ec_step5_items_by_ticker[t] = ec_items
        insider_count = int(insider.get("trade_count") or 0)
        if not biz_signals and insider_count == 0 and not ec_items:
            no_coverage_tickers.append(t)
            continue
        coverage_tickers.append(t)
        parts = [f"【{t}】"]
        for s in biz_signals[:5]:
            title = str(s.get("title") or "")[:120]
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
        for it in ec_items[:5]:
            parts.append(f"- {it}")
        all_signals_brief.append('\n'.join(parts))

    # 覆盖声明（避免“其余公司无内容”被误读为“无动态”）
    total = len(per_company)
    lines.append(
        f"> **数据覆盖**：本周获取到Step 5相关数据的公司：{len(coverage_tickers)}/{total} 家"
        + (f"（{', '.join(coverage_tickers)}）" if coverage_tickers else "（无）")
    )
    if no_coverage_tickers:
        lines.append(
            "> 以下公司本周无Benzinga新闻且无异常Insider申报，且电话会未检出可归类为Step 5的新增业务条目，"
            f"不代表无业务动态，建议自行查阅IR页面：{', '.join(no_coverage_tickers)}"
        )
    lines.append("")

    if len(all_signals_brief) >= 1:
        briefs_text = '\n\n'.join(all_signals_brief)
        _allowed_tickers_s5 = ', '.join([rec.ticker for rec, *_ in per_company])
        _s5_guard = (
            "严格要求：只输出本板块内容。所有结论必须有管理层原话或具体数字依据。"
            "禁止推断、禁止投资建议、禁止输出额外板块或总结段落。\n"
            "输出格式要求：先2-4句连贯段落总结，再按主题或公司分组列支撑数据（小标题+bullet）；"
            "每条必须标注来源公司和具体数字/原话。"
        )
        _s5_common = f"""以下是板块各公司本周的新业务、收购并购及Insider交易信号：

{briefs_text}

本板块监控公司白名单（严格只能引用以下公司，禁止提及任何不在此列表中的公司名称或ticker）：
{_allowed_tickers_s5}
"""

        def _run_s5_block(_name: str, _task_rule: str) -> str:
            _prompt = f"你是资深行业研究分析师。\n{_s5_guard}\n\n{_s5_common}\n{_task_rule}"
            _txt = _chat5(_prompt, max_tokens=800, timeout=300.0)
            _round = 0
            while _round < 3 and (
                _is_truncated_llm_output(_txt) or len((_txt or "").strip()) < 40
            ):
                logger.warning(
                    "Step5[%s]疑似截断，尝试续写 round=%s",
                    _name,
                    _round,
                )
                try:
                    _cont = _chat5(
                        f"以下是未完成的「{_name}」内容，请从中断处继续，只输出续写内容，不要重复已有内容：\n\n{_txt}",
                        max_tokens=800,
                        timeout=300.0,
                    )
                    _txt = _txt.rstrip() + "\n" + _cont
                except Exception:
                    logger.warning("Step5[%s]续写失败，使用已有内容", _name)
                    break
                _round += 1
            return (_txt or "").strip()

        try:
            s5_invest = _run_s5_block(
                "重大投资与收购",
                "第1次——【重大投资与收购】\n只列出有Benzinga新闻链接或Earning Call原文支撑的收购、投资事件。"
                "格式：[公司代码] 事件描述（金额/规模）— 来源：Benzinga/Earning Call。"
                "按统一格式输出（先2-4句段落总结，再分组小标题+bullet支撑数据），禁止推断战略意义，禁止额外内容。",
            )
            s5_strategy = _run_s5_block(
                "重大战略合作与其他动态",
                "第2次——【重大战略合作与其他动态】\n只列出有Earning Call原文或Form 4申报支撑的战略合作、Insider交易。"
                "格式：[公司代码] 事件描述 — 来源：Earning Call/Form 4。"
                "按统一格式输出（先2-4句段落总结，再分组小标题+bullet支撑数据），禁止推断，禁止额外内容。",
            )
            sector_signal = (
                "【重大投资与收购】\n"
                f"{s5_invest or '- （无可提取内容）'}\n\n"
                "【重大战略合作与其他动态】\n"
                f"{s5_strategy or '- （无可提取内容）'}"
            )
            # 扫描输出中是否包含非白名单公司 ticker；并对全文逐行强制过滤（续写后再次过滤）
            _allowed_set_s5 = {rec.ticker.upper() for rec, *_ in per_company}
            _brk_syms = {
                x.upper()
                for x in re.findall(
                    r"\[([A-Za-z]{2,6})\]", sector_signal or ""
                )
            }
            _word_syms = set(re.findall(r"\b[A-Z]{2,6}\b", sector_signal or ""))
            _rogue = (_brk_syms - _allowed_set_s5) | (
                _word_syms - _allowed_set_s5 - _COMMON_ABBREVS
            )
            if _rogue:
                logger.warning(
                    "Step5 LLM输出包含非白名单实体: %s，执行过滤",
                    _rogue,
                )
            sector_signal = _sanitize_bracket_tickers(sector_signal)
            sector_signal = _filter_by_ticker_whitelist(sector_signal, _allowed_set_s5)

            def _strip_step5_embedded_disclaimers(_body: str) -> str:
                """去掉模型在 sector 正文中复读的免责/来源行，与下方固定两行只保留一处。"""
                out_ln: list[str] = []
                for raw in (_body or "").splitlines():
                    s = raw.strip()
                    if "以下为系统辅助总结" in s and "仅供参考" in s:
                        continue
                    if "非原文直接提取" in s and "仅供参考" in s:
                        continue
                    if "原文链接请展开各公司详情" in s:
                        continue
                    if (
                        "Benzinga" in s
                        and "Insider" in s
                        and ("数据来源" in s or "**数据来源**" in s or "评判标准" in s)
                    ):
                        continue
                    out_ln.append(raw)
                return "\n".join(out_ln)

            sector_signal = _strip_step5_embedded_disclaimers(sector_signal)
            lines.append("> **数据来源**：Benzinga 公司新闻 + FMP Insider 交易申报（Form 4）｜**评判标准**：收购/战略合作/异常 Insider 交易（买入>$1M 或卖出>$5M）")
            lines.append("> ⚠️ **以下为系统辅助总结，非原文直接提取，仅供参考。原文链接请展开各公司详情查看。**")
            lines.append("")
            lines.append(sector_signal)
            lines.append("")
            if sector_summary_only:
                return lines

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
                ec_items_ref = ec_step5_items_by_ticker.get(t, [])
                if not biz_signals and insider_count == 0 and not ec_items_ref:
                    continue
                ref_parts = []
                if biz_signals:
                    ref_parts.append(f"Benzinga 新闻 {len(biz_signals)} 条")
                if ec_items_ref:
                    ref_parts.append(f"Earning Call 逐字稿 {len(ec_items_ref)} 条")
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
        except Exception as e:
            logger.exception("Step5 sector总结失败，错误详情：%s", e)
            if _strict_expose_llm_errors():
                raise
    # ── Sector 总结 END ──────────────────────────────────────────

    if sector_summary_only:
        return lines

    for rec, signals, insider, _below, _had in per_company:
        t = rec.ticker
        disp = company_display_name(t, rec.company_name)
        biz_signals = [
            s
            for s in signals
            if str(s.get("signal_type") or "")
            in ("business_change", "insider_trade")
        ]
        ec_items = ec_step5_items_by_ticker.get(t, [])
        if not biz_signals and int(insider.get("trade_count") or 0) == 0 and not ec_items:
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
        if ec_items:
            lines.append("**电话会补充（新业务/战略动向）：**")
            lines.append("")
            for it in ec_items:
                lines.append(f"- {it}")
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
    lines: list[str] = ["## Step 6｜财务数据（年度）", ""]

    company_data: list[tuple[str, str, list[Any]]] = []
    validation_issues: list[str] = []
    all_years: list[int] = []
    for rec, _signals, _insider, _below, _had in per_company:
        t = rec.ticker
        disp = company_display_name(t, rec.company_name)
        try:
            rows, issues = _get_validated_financials(t, years=years)
            validation_issues.extend(issues)
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
    # 检查是否存在非日历年财年（如MDB/ZM财年截止1月31日，会早一年）
    current_cal_year = datetime.now(timezone.utc).year
    if current_cal_year in all_years and len(all_years) > 1:
        lines.append(
            f"> 📅 **财年说明**：FY{current_cal_year} 列仅含非日历财年公司"
            f"（如 MDB、ZM 等财年截止于1月31日）；"
            f"其余公司最新完整财年为 FY{current_cal_year - 1}，"
            f"FY{current_cal_year} 数据尚未公布，显示为 —。"
        )
        lines.append("")

    for metric_name, metric_fn in [
        ("Revenue", lambda r: fmt_b(r.revenue)),
        ("Gross Margin", lambda r: fmt_pct(r.gross_margin)),
        ("Net Income", lambda r: fmt_b(r.net_income)),
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
    total_companies_count = len(company_data)
    lines.append(f"| 指标 | {year_headers} |")
    lines.append(sep_line)
    for metric_name, attr in [("Total Revenue", "revenue"), ("Total CAPEX", "capex")]:
        vals_out: list[str] = []
        for y in all_years:
            total = 0.0
            has_count = 0
            for _, _, rows in company_data:
                row_by_year = {r.year: r for r in rows}
                if y in row_by_year:
                    v = getattr(row_by_year[y], attr)
                    if v is not None:
                        if attr == "capex":
                            total += abs(float(v))
                        else:
                            total += float(v)
                        has_count += 1
            # 仅当超过半数公司有数据时才输出汇总，否则标注实际覆盖数
            if has_count == 0:
                vals_out.append("—")
            elif has_count < total_companies_count // 2 + 1:
                vals_out.append(f"{fmt_b(total)}*")
            else:
                vals_out.append(fmt_b(total))
        lines.append(f"| **{metric_name}** | {' | '.join(vals_out)} |")
    lines.append("")
    lines.append(
        "*数据来源：FMP Annual Financials。Net Debt/Equity = (总债务-现金)/股东权益。"
        "\\* 标记表示该年度覆盖公司不足半数，汇总仅供参考。*"
    )
    lines.append("")
    if validation_issues:
        lines.append("> ⚠️ **财务字段合理性校验提示（自动检测）：**")
        for msg in validation_issues[:20]:
            lines.append(f"> * {msg}；对应异常字段已暂停自动填入（显示为 —）。")
        lines.append("")

    # 异常数据自动检测注释块
    notes: list[str] = []

    def _gm_to_ratio(v: float | None) -> float | None:
        if v is None:
            return None
        try:
            fv = float(v)
        except (TypeError, ValueError):
            return None
        # 兼容 0.38 / 38 两种口径
        if abs(fv) > 2:
            fv = fv / 100.0
        return fv

    for t, _disp, rows in company_data:
        row_by_year = {getattr(r, "year", None): r for r in rows}
        available_years = [
            y for y in all_years if y in row_by_year and row_by_year.get(y) is not None
        ]
        if len(available_years) >= 2:
            current_year = available_years[0]
            prior_year = available_years[1]
            current_row = row_by_year.get(current_year)
            prior_row = row_by_year.get(prior_year)
            if current_row is not None and prior_row is not None:
                # (1) 毛利率同比变化超过 ±300bps
                gm_current = _gm_to_ratio(getattr(current_row, "gross_margin", None))
                gm_prior = _gm_to_ratio(getattr(prior_row, "gross_margin", None))
                if gm_current is not None and gm_prior is not None:
                    if abs(gm_current - gm_prior) > 0.03:
                        flag = "↓" if gm_current < gm_prior else "↑"
                        notes.append(
                            f"* {t} 毛利率 FY{prior_year}→FY{current_year} {flag}{abs(gm_current-gm_prior)*10000:.0f}bps，"
                            "变化幅度较大，请核查是否涉及会计口径调整或一次性项目。"
                        )

                # (2) 净利润同比变化超过 ±50%（且绝对值超过$1亿）
                ni_current_raw = getattr(current_row, "net_income", None)
                ni_prior_raw = getattr(prior_row, "net_income", None)
                if ni_current_raw is None or ni_prior_raw is None:
                    ni_current = None
                    ni_prior = None
                else:
                    try:
                        ni_current = float(ni_current_raw) / 1e9
                        ni_prior = float(ni_prior_raw) / 1e9
                    except (TypeError, ValueError):
                        ni_current = None
                        ni_prior = None
                if (
                    ni_current is not None
                    and ni_prior is not None
                    and abs(ni_prior) > 1e-9
                    and abs(ni_current - ni_prior) / abs(ni_prior) > 0.5
                    and abs(ni_current) > 0.1
                ):
                    chg = ((ni_current - ni_prior) / abs(ni_prior)) * 100
                    notes.append(
                        f"* {t} 净利润 FY{prior_year}→FY{current_year} 变化{chg:+.0f}%，"
                        "请确认是否含一次性项目（商誉减值、资产处置、税务调整等）。"
                    )

                # (4) 净利润盈利/亏损切换
                if (
                    ni_current is not None
                    and ni_prior is not None
                    and ((ni_prior > 0 and ni_current < 0) or (ni_prior < 0 and ni_current > 0))
                ):
                    notes.append(
                        f"* {t} 净利润由{'盈利转亏损' if ni_current < 0 else '亏损转盈利'}，请核查转折原因。"
                    )

        # (3) Net Debt/Equity 绝对值超过 5x（按可用年份逐年检查）
        for y in available_years:
            r = row_by_year.get(y)
            if r is None:
                continue
            nd_eq_raw = getattr(r, "net_debt_to_equity", None)
            if nd_eq_raw is None:
                continue
            try:
                nd_eq = float(nd_eq_raw)
            except (TypeError, ValueError):
                continue
            if abs(nd_eq) > 5:
                notes.append(
                    f"* {t} Net Debt/Equity FY{y} = {nd_eq:.2f}x，为极端值，"
                    "通常反映股东权益接近零或为负，建议结合资产负债表原始数据核实。"
                )

    if notes:
        lines.append("> ⚠️ **数据异常提示（自动检测）：**")
        for n in notes:
            lines.append(f"> {n}")
        lines.append("")
    return lines


def _step6_quarterly_charts_section(
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
) -> dict[str, Any]:
    """
    拉取所有公司季度数据，返回供前端渲染的结构：
    {
        "sector_quarterly": {ticker: [QuarterlyFinancials]},  # 板块汇总用
        "per_company_quarterly": {ticker: [QuarterlyFinancials]},  # 各公司折叠用
        "sector_name": sector,
    }
    """
    from research_automation.extractors.fmp_client import get_quarterly_financials

    per_company_quarterly: dict[str, list] = {}
    for rec, *_ in per_company:
        t = rec.ticker
        try:
            rows = get_quarterly_financials(t, quarters=8)
            if rows:
                per_company_quarterly[t] = rows
        except Exception:
            logger.exception("季度数据拉取失败 ticker=%s", t)

    return {
        "sector_quarterly": per_company_quarterly,
        "per_company_quarterly": per_company_quarterly,
        "sector_name": sector,
    }


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


def _is_exec_summary_rank_heading(line: str) -> bool:
    s = line.strip()
    return s.startswith("###") and "🏆" in s and "相对强弱" in s


def _exec_summary_ranking_section_line_ranges(lines: list[str]) -> list[tuple[int, int]]:
    """每个 [start, end) 为半开区间，对应一段「### 🏆 …相对强弱…」板块（含表体）。"""
    ranges: list[tuple[int, int]] = []
    i = 0
    n = len(lines)
    while i < n:
        if not _is_exec_summary_rank_heading(lines[i]):
            i += 1
            continue
        start = i
        i += 1
        while i < n and not _is_exec_summary_rank_heading(lines[i]):
            st = lines[i].strip()
            if st.startswith("###"):
                break
            i += 1
        ranges.append((start, i))
    return ranges


def _normalize_exec_summary_ranking_sections(
    summary: str, *, canonical_ranking_md: str
) -> str:
    """
    保证最终只保留一张「相对强弱排序」表：
    - 若下方有代码生成的 canonical 表，则整段删除主摘要 LLM（含续写）里所有同名板块，避免与 canonical 重复。
    - 若无 canonical，仅删除续写导致的第二段及之后同名板块，保留第一段。
    """
    lines = summary.split("\n")
    ranges = _exec_summary_ranking_section_line_ranges(lines)
    if not ranges:
        return summary
    if canonical_ranking_md.strip():
        merged = lines[: ranges[0][0]] + lines[ranges[-1][1] :]
        return "\n".join(merged)
    if len(ranges) <= 1:
        return summary
    merged = lines[: ranges[0][1]] + lines[ranges[-1][1] :]
    return "\n".join(merged)


def _executive_summary(
    sector: str,
    step4_lines: list[str],
    step5_lines: list[str],
    step6_lines: list[str],
    sector_watch_items: list[str] | None = None,
    per_company: list | None = None,
) -> list[str]:
    """执行摘要：汇总Earning Call、新业务、财务数据，生成sector级别的执行摘要。"""
    from research_automation.extractors.llm_client import chat_with_retry
    from research_automation.extractors.fmp_client import get_financials
    from research_automation.services.earnings_service import (
        EarningsAnalysisError,
        analyze_earnings_call,
    )
    from research_automation.services.profile_service import (
        ProfileGenerationError,
        get_profile,
    )
    from .post_generation_checker import (
        build_baseline_from_rows,
        run_post_generation_check,
    )

    # 抽取关键内容（控制token）
    def _extract_key_lines(
        lines: list[str],
        max_lines: int = 60,
        *,
        per_company: list | None = None,
    ) -> str:
        filtered = [
            l
            for l in lines
            if l.strip() and not l.strip().startswith("|---")
        ]
        if not filtered:
            return ""
        if per_company is None:
            return "\n".join(filtered[:max_lines])

        # Step4：按 ### TICKER — 分段，每家公司最多 15 行，再补足至 max_lines
        import re as _re_es

        def _header_ticker(line: str) -> str | None:
            s = line.strip()
            m = _re_es.match(r"^###\s+(.+?)\s+—\s*", s)
            if m:
                return m.group(1).strip().upper()
            m = _re_es.match(r"^###\s+(\S+)", s)
            return m.group(1).strip().upper() if m else None

        block_starts: list[int] = []
        for i, line in enumerate(filtered):
            if _header_ticker(line):
                block_starts.append(i)
        ticker_to_range: dict[str, tuple[int, int]] = {}
        for bi, start in enumerate(block_starts):
            end = (
                block_starts[bi + 1]
                if bi + 1 < len(block_starts)
                else len(filtered)
            )
            t = _header_ticker(filtered[start])
            if t and t not in ticker_to_range:
                ticker_to_range[t] = (start, end)

        picked_idx: list[int] = []
        picked_set: set[int] = set()
        remaining = max_lines

        sym_order: list[str] = []
        for rec in per_company:
            sym = (getattr(rec, "ticker", None) or "").strip().upper()
            if sym and sym not in sym_order:
                sym_order.append(sym)

        for sym in sym_order:
            if remaining <= 0:
                break
            blk = ticker_to_range.get(sym)
            if not blk:
                continue
            start, end = blk
            take = min(20, end - start, remaining)
            for j in range(take):
                idx = start + j
                if idx not in picked_set:
                    picked_idx.append(idx)
                    picked_set.add(idx)
                    remaining -= 1
                if remaining <= 0:
                    break

        i = 0
        while len(picked_set) < max_lines and i < len(filtered):
            if i not in picked_set:
                picked_idx.append(i)
                picked_set.add(i)
            i += 1

        picked_idx.sort()
        out_lines = [filtered[k] for k in picked_idx[:max_lines]]
        return "\n".join(out_lines)

    step4_text = _extract_key_lines(step4_lines, 150, per_company=per_company)
    step5_text = _extract_key_lines(step5_lines, 30)
    step6_text = _extract_key_lines(step6_lines, 40)
    from research_automation.core import sector_config as _sector_config
    _get_tickers = getattr(_sector_config, "get_tickers", None)
    if callable(_get_tickers):
        _raw = _get_tickers(sector)
        if isinstance(_raw, (list, tuple, set)):
            _allowed_set_exec = {str(x).strip().upper() for x in _raw if str(x).strip()}
        else:
            _allowed_set_exec = set()
    else:
        _allowed_set_exec = {
            (getattr(rec, "ticker", "") or "").strip().upper()
            for rec, *_ in (per_company or [])
        }
    step4_text = _filter_by_ticker_whitelist(step4_text, _allowed_set_exec)
    step5_text = _filter_by_ticker_whitelist(step5_text, _allowed_set_exec)

    watch_str = "、".join(sector_watch_items) if sector_watch_items else "无"
    struct_notes = _special_structure_guidance_for_tickers(
        [getattr(rec, "ticker", "") for rec, *_ in (per_company or [])]
    )

    # 收集财务快照
    financial_snapshot = ""
    baseline: dict[str, Any] = {}
    if per_company:
        import statistics as _stats

        yoy_list, gm_list = [], []
        company_snaps = []
        for rec, *_ in per_company:
            try:
                rows, _issues = _get_validated_financials(rec.ticker, years=2)
                baseline.update(build_baseline_from_rows(rec.ticker, rows))
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
            total_companies = len(per_company)
            yoy_sample = len(yoy_list)
            gm_sample = len(gm_list)
            financial_snapshot = f"""
Sector财务快照：
- 监控公司总数：{total_companies} 家，其中有效Revenue YoY样本：{yoy_sample} 家，有效Gross Margin样本：{gm_sample} 家
- 中位Revenue YoY（基于{yoy_sample}家）：{median_yoy:+.1f}%
- 中位Gross Margin（基于{gm_sample}家）：{gm_disp}
- 增速前三：{", ".join(top3)}
- 增速后三：{", ".join(bot3)}
"""
            # 从 step4_lines 中提取各公司指引方向（上调/下调/维持/撤回）
            # 用正则匹配「上调」「下调」「维持」「撤回」关键词，与 ticker 在同一行出现时记录
            import re as _re_fin

            guidance_direction: dict[str, str] = {}
            for line in (step4_lines or []):
                for _tk in ([rec.ticker for rec, *_ in (per_company or [])]):
                    if _tk in line:
                        if any(w in line for w in ["上调", "raised", "raise"]):
                            guidance_direction[_tk] = "上调"
                        elif any(w in line for w in ["下调", "lowered", "lower", "cut"]):
                            guidance_direction[_tk] = "下调"
                        elif any(w in line for w in ["撤回", "withdrew", "withdrawn"]):
                            guidance_direction[_tk] = "撤回"
                        elif any(w in line for w in ["维持", "maintained", "reiterated"]):
                            guidance_direction[_tk] = "维持"

            # 将指引方向拼入 financial_snapshot
            if guidance_direction:
                guidance_lines = "\n".join(
                    f"  {tk}：指引{direction}"
                    for tk, direction in guidance_direction.items()
                )
                financial_snapshot += f"\n- 本季指引变动方向：\n{guidance_lines}"

    _strict_guard = (
        "严格要求：只输出本板块内容，不得输出其他板块、附录、术语表、结语或任何额外内容。"
        "所有结论必须有管理层原话或具体数字作为依据，禁止推断和投资建议。\n"
        "输出格式要求：\n"
        "1. 先用2-4句连贯的段落文字，对本板块内容做整体归纳总结，必须基于具体数据、提及主要公司与关键数字。\n"
        "2. 再用分组方式列出支撑数据，每组有小标题，组内用bullet point。\n"
        "3. 每条bullet必须标注来源公司和具体数字/原话，按主题或公司分组，避免平铺。"
    )
    _common_data_block = f"""
本sector重点关注项：{watch_str}

{struct_notes}

【财务快照】
{financial_snapshot}

【Earning Call 摘录】
{step4_text}

【新业务/收购/Insider 摘录】
{step5_text}

【财务数据摘录】
{step6_text}
"""

    def _build_section_prompt(_title: str, _rule: str) -> str:
        return (
            f"你是资深行业研究分析师。请仅生成执行摘要中的「{_title}」板块正文。\n"
            f"{_strict_guard}\n\n"
            f"{_common_data_block}\n\n"
            f"{_rule}\n\n"
            "禁止输出标题、分隔线、表格、额外说明；只输出本板块正文内容。"
        )

    def _run_exec_section(
        section_name: str,
        prompt: str,
        *,
        max_tokens: int,
        min_len: int = 80,
    ) -> str:
        _plen = len(prompt)
        logger.info(
            "执行摘要子板块[%s] prompt长度：%s 字符，约 %s tokens（粗估 chars/4）",
            section_name,
            _plen,
            _plen // 4,
        )
        text = chat_with_retry(prompt, max_tokens=max_tokens, timeout=300.0)
        _cont_round = 0
        while _cont_round < 3 and (
            _is_truncated_llm_output(text) or len((text or "").strip()) < min_len
        ):
            logger.warning(
                "执行摘要子板块[%s]疑似截断，尝试续写 round=%s",
                section_name,
                _cont_round,
            )
            _cont_prompt = (
                f"以下是未完成的「{section_name}」板块正文，请从中断处继续，只输出续写内容，不要重复已有内容：\n\n"
                f"{text}"
            )
            try:
                _cont = chat_with_retry(
                    _cont_prompt,
                    max_tokens=max_tokens,
                    timeout=300.0,
                )
                text = text.rstrip() + "\n" + _cont
            except Exception:
                logger.warning("执行摘要子板块[%s]续写失败，使用已有内容", section_name)
                break
            _cont_round += 1
        return (text or "").strip()

    _min_coverage = max(3, int(len(per_company or []) * 0.6))
    _coverage_rule = (
        f"覆盖要求：本板块内容必须覆盖至少{_min_coverage}家公司，"
        "不得集中于AI/科技类公司，零售、工业、物流类公司也必须有具体数据支撑。"
        "若某公司无Earning Call数据，则使用财务快照数据（Revenue/EPS/YoY）作为替代。"
    )

    try:
        sec_fin_prompt = _build_section_prompt(
            "📊 财务快照",
            "任务要求：基于上方【财务快照】中已计算好的行业中位数和各公司数据，"
            "写一段150字以内的行业横向总结，要求："
            "① 点出sector增速中位数和分布区间，② 点出增速最高和最低各1-2家及原因，"
            "③ 结合「本季指引变动方向」字段，说明哪些公司指引上调/下调，"
            "禁止自行引用任何未在【财务快照】数据中出现的数字，"
            "禁止逐家罗列公司数字（Step6已有详细表格）。"
            + _coverage_rule,
        )
        sec_theme_prompt = _build_section_prompt(
            "🔑 本季核心主题",
            "任务要求：只输出跨3家以上公司出现的共同表述，每条必须附涉及公司名和原始数字，禁止推断。"
            + _coverage_rule,
        )
        sec_event_prompt = _build_section_prompt(
            "⚡ 重要事件",
            "任务要求：只输出有原话/新闻/数字支撑的实质性事件，以编号列表输出。"
            + _coverage_rule,
        )
        sec_signal_prompt = _build_section_prompt(
            "💬 管理层关键信号",
            "任务要求：只输出有发言人姓名+原话的表述，格式为「公司 — 发言人：原话」。"
            "严格要求：发言人姓名必须逐字来自上方【Earning Call 摘录】中出现的真实姓名，"
            "禁止使用任何未在摘录中出现的姓名，禁止从记忆或训练数据补充任何人名。"
            "若某公司摘录中未出现具名发言人，则该公司不得出现在本板块。",
        )
        sec_risk_prompt = _build_section_prompt(
            "⚠️ 主要风险",
            "任务要求：只输出管理层主动披露的不确定因素，每条必须附公司名和具体表述依据。",
        )

        sec_fin_text = _run_exec_section("📊 财务快照", sec_fin_prompt, max_tokens=800)
        sec_theme_text = _run_exec_section("🔑 本季核心主题", sec_theme_prompt, max_tokens=1200)
        sec_event_text = _run_exec_section("⚡ 重要事件", sec_event_prompt, max_tokens=800)
        sec_signal_text = _run_exec_section("💬 管理层关键信号", sec_signal_prompt, max_tokens=600)
        sec_risk_text = _run_exec_section("⚠️ 主要风险", sec_risk_prompt, max_tokens=800)

        # 执行摘要白名单过滤：剔除包含非监控标的 ticker 的行（仅作用于「重要事件/管理层关键信号」）
        from research_automation.core import sector_config

        _get_tickers = getattr(sector_config, "get_tickers", None)
        if callable(_get_tickers):
            _sector_tickers_raw = _get_tickers(sector)
            if isinstance(_sector_tickers_raw, (list, tuple, set)):
                SECTOR_TICKERS = {
                    str(x).strip().upper()
                    for x in _sector_tickers_raw
                    if str(x).strip()
                }
            else:
                SECTOR_TICKERS = set()
        else:
            # 兼容：若 sector_config 未提供 get_tickers，则用当前报告覆盖公司作为白名单
            SECTOR_TICKERS = {
                (getattr(rec, "ticker", "") or "").strip().upper()
                for rec, *_ in (per_company or [])
                if (getattr(rec, "ticker", "") or "").strip()
            }

        def _filter_non_sector_content(text: str, allowed_tickers: set[str]) -> str:
            """过滤掉包含非监控标的的段落"""
            lines = (text or "").split("\n")
            filtered: list[str] = []
            for line in lines:
                skip = False
                for word in line.split():
                    clean = word.strip('*[]().,:-—"""')
                    if len(clean) >= 2 and len(clean) <= 5 and clean.isupper() and clean.isalpha():
                        if clean not in allowed_tickers and clean not in {
                            'AI', 'IT', 'LLM', 'API', 'CEO', 'CFO', 'COO', 'EPS', 'ARR', 'NRR',
                            'TCV', 'RPO', 'AUM', 'YOY', 'FMP', 'SEC', 'ESG', 'IPO', 'M&A', 'PE', 'VC'
                        }:
                            skip = True
                            break
                if not skip:
                    filtered.append(line)
            return '\n'.join(filtered)

        sec_event_text = _filter_non_sector_content(sec_event_text, SECTOR_TICKERS)
        sec_signal_text = _filter_non_sector_content(sec_signal_text, SECTOR_TICKERS)

        summary_parts = [
            "### 📊 财务快照",
            "> 数据来源：FMP Annual Financials | 评判标准：最新财年Revenue YoY增速中位数及分布",
            sec_fin_text or "（无可用内容）",
            "",
            "---",
            "",
            "### 🔑 本季核心主题",
            "> 数据来源：Earning Call 逐字稿（FMP/SEC EDGAR）| 评判标准：跨3家以上公司出现的共同表述",
            sec_theme_text or "（无可用内容）",
            "",
            "---",
            "",
            "### ⚡ 重要事件",
            "> 数据来源：Earning Call 逐字稿 + Benzinga 公司新闻 | 评判标准：涉及资本配置/人员/产品的实质性变化",
            sec_event_text or "（无可用内容）",
            "",
            "---",
            "",
            "### 💬 管理层关键信号",
            "> 数据来源：Earning Call 逐字稿原话 | 评判标准：CEO/CFO对业务趋势的直接表态",
            sec_signal_text or "（无可用内容）",
            "",
            "---",
            "",
            "### ⚠️ 主要风险",
            "> 数据来源：Earning Call 前瞻性表述（含 expect/may/consider 等情态动词）| 评判标准：管理层主动披露的不确定因素",
            sec_risk_text or "（无可用内容）",
        ]
        summary = "\n".join(summary_parts).strip()
        # ── 排名表：代码控制公司列表，LLM只填优势和风险 ──────────
        ranking_table = ""
        if per_company:
            # 收集有财务数据的公司
            ranked_tickers = []
            skipped_tickers = []
            for rec, *_ in per_company:
                try:
                    rows, _ = _get_validated_financials(rec.ticker, years=1)
                    if rows:
                        latest = max(rows, key=lambda r: getattr(r, "year", 0) or 0)
                        rev = getattr(latest, "revenue", None)
                        # 必须有有效Revenue才能参与排名
                        if rev is not None and float(rev) > 0:
                            ranked_tickers.append(rec.ticker)
                        else:
                            skipped_tickers.append(rec.ticker)
                            logger.info(
                                "排名表跳过 %s：Revenue 不可用或为零",
                                rec.ticker,
                            )
                except Exception:
                    skipped_tickers.append(rec.ticker)

            if ranked_tickers:
                # 构建每家公司的财务摘要供LLM参考
                company_contexts = []
                for t in ranked_tickers:
                    try:
                        rows, _ = _get_validated_financials(t, years=2)
                        if not rows:
                            company_contexts.append(f"{t}: 财务数据不可用")
                            continue
                        latest = max(rows, key=lambda r: getattr(r, "year", 0) or 0)
                        prev_list = [r for r in rows if (getattr(r, "year", 0) or 0) < (getattr(latest, "year", 0) or 0)]
                        prev = max(prev_list, key=lambda r: getattr(r, "year", 0) or 0) if prev_list else None
                        rev = getattr(latest, "revenue", None)
                        prev_rev = getattr(prev, "revenue", None) if prev else None
                        yoy = ((float(rev) - float(prev_rev)) / abs(float(prev_rev)) * 100) if rev and prev_rev and prev_rev != 0 else None
                        gm = getattr(latest, "gross_margin", None)
                        ni = getattr(latest, "net_income", None)
                        ctx = f"{t}: Revenue ${float(rev)/1e9:.1f}B" if rev else f"{t}:"
                        if yoy is not None:
                            ctx += f" YoY {yoy:+.1f}%"
                        if gm is not None:
                            gm_val = float(gm) * 100 if float(gm) < 2 else float(gm)
                            ctx += f", GM {gm_val:.1f}%"
                        if ni is not None:
                            ctx += f", NI ${float(ni)/1e9:.2f}B"
                        company_contexts.append(ctx)
                    except Exception:
                        company_contexts.append(f"{t}: 数据获取失败")

                ranking_prompt = f"""你是资深行业研究分析师。以下是{sector}板块各公司的财务数据摘要：

{chr(10).join(company_contexts)}

以下是本季度Earning Call和新业务摘录（供参考）：
{step4_text[:800]}

请为以下每家公司生成排名评估，返回严格的JSON，不要任何额外文字和markdown：

{{
  "rankings": [
    {{
      "ticker": "CTSH",
      "rank": 1,
      "core_advantage": "核心优势描述（附具体数字，30字以内）",
      "main_risk": "主要风险描述（20字以内）"
    }}
  ]
}}

必须覆盖以下全部 {len(ranked_tickers)} 家公司，按基本面强弱从强到弱排序，一家都不能省略：
{', '.join(ranked_tickers)}

要求：
1. 每家公司必须出现，不得跳过任何一家
2. core_advantage 必须包含至少一个具体数字
3. 只基于提供的数据，不捏造
4. 排名靠前的公司优势要明显强于靠后的"""

                try:
                    ranking_raw = chat_with_retry(ranking_prompt, max_tokens=2200, timeout=300.0)
                    ranking_data = _parse_llm_json_payload(ranking_raw)
                    if ranking_data and "rankings" in ranking_data:
                        rankings = ranking_data["rankings"]
                        # 确保所有公司都在，补全缺失的
                        covered = {r.get("ticker", "").upper() for r in rankings}
                        for t in ranked_tickers:
                            if t.upper() not in covered:
                                rankings.append({
                                    "ticker": t,
                                    "rank": len(rankings) + 1,
                                    "core_advantage": "财务数据可用，本季Earning Call覆盖有限",
                                    "main_risk": "数据覆盖不足，需人工补充"
                                })
                        # 按rank排序
                        rankings.sort(key=lambda x: int(x.get("rank", 99)))
                        # 重新编号确保连续
                        _skip_note = f"（以下公司因财务数据不足未参与排名：{', '.join(skipped_tickers)}）" if skipped_tickers else ""
                        table_lines = [
                            "### 🏆 相对强弱排序",
                            f"> 数据来源：综合财务快照 + 管理层前瞻指引 + 重要事件 | 评判标准：当前基本面动能与指引置信度的综合评估 {_skip_note}",
                            "",
                            "| 排名 | 公司 | 核心优势 | 主要风险 |",
                            "|------|------|----------|----------|",
                        ]
                        for i, r in enumerate(rankings, 1):
                            ticker = r.get("ticker", "")
                            adv = r.get("core_advantage", "—").replace("|", "｜")
                            risk = r.get("main_risk", "—").replace("|", "｜")
                            table_lines.append(f"| {i} | **{ticker}** | {adv} | {risk} |")
                        table_lines.append("")
                        ranking_table = "\n".join(table_lines)
                    else:
                        logger.warning("排名表LLM返回JSON解析失败，使用降级方案")
                        # 降级：只用公司列表生成空表
                        _skip_note = f"（以下公司因财务数据不足未参与排名：{', '.join(skipped_tickers)}）" if skipped_tickers else ""
                        table_lines = [
                            "### 🏆 相对强弱排序",
                            f"> 数据来源：综合财务快照 + 管理层前瞻指引 | 评判标准：财务数据 {_skip_note}",
                            "",
                            "| 排名 | 公司 | 核心优势 | 主要风险 |",
                            "|------|------|----------|----------|",
                        ]
                        for i, t in enumerate(ranked_tickers, 1):
                            table_lines.append(f"| {i} | **{t}** | — | — |")
                        table_lines.append("")
                        ranking_table = "\n".join(table_lines)
                except Exception:
                    logger.exception("排名表生成失败，跳过")
        # ── 排名表生成结束 ──────────────────────────────────────
        try:
            checker_result = run_post_generation_check(
                texts={"executive_summary": summary},
                baseline=baseline,
            )
            if checker_result["summary"]["error"] > 0:
                logger.info(
                    "[验证层C] 执行摘要发现 %s 处疑似错误，见 findings",
                    checker_result["summary"]["error"],
                )
        except Exception:
            logger.exception("执行摘要后置数字核查失败，已跳过")
    except Exception as e:
        logger.exception("执行摘要LLM调用失败，错误详情：%s", e)
        if _strict_expose_llm_errors():
            raise
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

    summary = _normalize_exec_summary_ranking_sections(
        summary, canonical_ranking_md=ranking_table
    )

    lines_out: list[str] = [
        "## 📋 执行摘要",
        "",
        "> ⚠️ **以下执行摘要为系统基于财务数据及Earning Call逐字稿的辅助总结，非原文直接提取，仅供参考。各项结论的原始依据请查阅各Step详情中的quotations及数据来源。**",
        "",
        summary,
        "",
        ranking_table,
        "\n".join(ref_lines),
        "",
        "---",
        "",
    ]
    # 增加“分析层面缺失”透明提示：财务有数据，但 Step3/4 分析不可用
    if per_company:
        now = datetime.now(timezone.utc)
        ec_year = now.year
        ec_quarter = (now.month - 1) // 3 + 1
        if ec_quarter == 1:
            ec_quarter = 4
            ec_year -= 1
        else:
            ec_quarter -= 1

        missing_analysis: list[str] = []
        for rec, *_ in per_company:
            t = rec.ticker
            try:
                has_fin = bool(get_financials(t, years=1))
            except Exception:
                has_fin = False
            if not has_fin:
                continue
            profile_ok = True
            earnings_ok = True
            try:
                get_profile(t)
            except (ProfileGenerationError, Exception):
                profile_ok = False
            try:
                analyze_earnings_call(t, ec_year, ec_quarter)
            except (EarningsAnalysisError, Exception):
                earnings_ok = False
            if not profile_ok or not earnings_ok:
                missing_analysis.append(t)

        if missing_analysis:
            lines_out.insert(
                4,
                (
                    "> ⚠️ 以下公司财务数据可用，但Earning Call逐字稿获取失败或10-K画像不可用，"
                    f"Step 3/4分析存在缺失：**{', '.join(missing_analysis)}**"
                ),
            )
            lines_out.insert(5, "")
    return lines_out


def _step_overview_sector(
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
) -> list[str]:
    """板块概览：公司数量、业务类型分布、数据覆盖情况。"""
    from research_automation.extractors.llm_client import chat_with_retry as _chat
    from research_automation.services.profile_service import (
        ProfileGenerationError,
        get_profile,
    )

    from research_automation.extractors.fmp_client import get_financials

    total = len(per_company)

    # 区分"有效覆盖"与"数据暂不可用"公司
    # 判断标准：能拉到至少1年财务数据 = 有效；否则列为数据缺失
    data_available: list[str] = []
    data_unavailable: list[str] = []
    for rec, *_ in per_company:
        try:
            rows = get_financials(rec.ticker, years=1)
            if rows:
                data_available.append(rec.ticker)
            else:
                data_unavailable.append(rec.ticker)
        except Exception:
            data_unavailable.append(rec.ticker)

    effective_count = len(data_available)
    unavailable_count = len(data_unavailable)

    lines: list[str] = [
        "## 📊 板块概览",
        "",
        (
            f"**板块**：{sector}　｜　"
            f"**监控公司总数**：{total} 家　｜　"
            f"**有效数据覆盖**：{effective_count} 家"
            + (
                f"　｜　**数据暂不可用**：{unavailable_count} 家（{', '.join(data_unavailable)}）"
                if data_unavailable else ""
            )
        ),
        "",
    ]
    if data_unavailable:
        lines.append(
            f"> ⚠️ 以下公司因非美股或SEC数据不可用，本报告各Step均无实质内容，"
            f"不计入有效覆盖统计：**{', '.join(data_unavailable)}**"
        )
        lines.append("")

    # 收集各公司核心业务描述
    company_biz: list[str] = []
    for rec, *_ in per_company:
        try:
            profile = get_profile(rec.ticker)
            cb = (profile.core_business or "").strip()[:150]
            if cb:
                company_biz.append(f"{rec.ticker}（{rec.company_name or rec.ticker}）：{cb}")
        except (ProfileGenerationError, Exception):
            company_biz.append(
                f"{rec.ticker}（{rec.company_name or rec.ticker}）：业务信息暂不可用"
            )

    if company_biz:
        biz_text = "\n".join(company_biz)
        prompt = f"""以下是{sector}板块各公司的核心业务描述：

{biz_text}

请将这{total}家公司按业务类型分组，生成板块业务构成概览。

要求：
1. 按业务类型分组，每组列出公司名称（ticker）
2. 每组用一句话描述该类型的共同特征
3. 格式：【业务类型】：ACN、BAH、CTSH（X家）— 简要描述
4. 所有公司必须出现在某个分组中，不得遗漏
5. 分组数量3-6个为宜，不要过度细分
6. 中文输出，ticker保留英文
7. 禁止输出任何以#开头的标题行"""

        _pov = len(prompt)
        logger.info(
            "板块概览（业务分组）prompt长度：%s 字符，约 %s tokens（粗估 chars/4）",
            _pov,
            _pov // 4,
        )

        try:
            overview = _chat(prompt, max_tokens=600, timeout=300.0)
            lines.append("**业务构成分布：**")
            lines.append("")
            lines.append(overview)
            lines.append("")
        except Exception as e:
            logger.exception("板块概览LLM调用失败，错误详情：%s", e)
            if _strict_expose_llm_errors():
                raise

    # 数据覆盖情况（用实际数据填充）
    lines.append("**数据覆盖情况：**")
    lines.append("")
    lines.append("| 数据项 | 覆盖公司数 | 说明 |")
    lines.append("|--------|------------|------|")
    lines.append(f"| 监控公司总数 | {total} 家 | 含所有活跃标的 |")
    lines.append(
        f"| 财务数据（FMP） | {effective_count} 家 | "
        + (f"缺失：{', '.join(data_unavailable)}" if data_unavailable else "全部覆盖")
        + " |"
    )
    lines.append("| 逐字稿（FMP/SEC/sec-api） | 见 Step 4 覆盖说明 | 实际覆盖数在Step4标题行显示 |")
    lines.append("")

    return lines


def _step_company_cards(
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
    """H-11：各公司业务介绍卡片，放在板块概览之后、执行摘要之前。"""
    from research_automation.services.profile_service import (
        ProfileGenerationError,
        get_profile,
    )

    lines: list[str] = [
        "## 🏢 各公司业务简介",
        "",
    ]

    for rec, *_ in per_company:
        t = rec.ticker
        disp = company_display_name(t, rec.company_name)
        lines.append(f"### {t} — {disp}")
        lines.append("")

        try:
            profile = get_profile(t)

            # 核心业务
            cb = (profile.core_business or "").strip()
            # 过滤提取失败的原始痕迹（如 ITEM_1_BUSINESS 章节标题残留）
            _EXTRACTION_FAILURE_MARKERS = (
                "ITEM_1_BUSINESS",
                "ITEM_7_MD",
                "原文中仅出现章节标题",
                "具体内容未在所提供段落中完整呈现",
            )
            _cb_has_failure_marker = any(m in cb for m in _EXTRACTION_FAILURE_MARKERS)
            if _cb_has_failure_marker:
                # 尝试截取失败标记之后的有效内容
                for marker in _EXTRACTION_FAILURE_MARKERS:
                    idx = cb.find(marker)
                    if idx != -1:
                        # 找到下一个句号或逗号之后的内容
                        after = cb[idx:]
                        end = after.find("），")
                        if end != -1:
                            cb = cb[idx + end + 2:].strip()
                        else:
                            # 找不到有效内容，直接置空
                            cb = ""
                        break
            # 额外判断：若 core_business 实质上只是分部数据罗列（含%且无完整句子），视为无效
            import re as _re_cb
            _cb_is_segment_only = (
                bool(_re_cb.search(r'\d+\.\d+%', cb))
                and len(cb) < 150
                and "。" not in cb
                and "，" not in cb[:50]
            )
            if cb and cb not in ("原文未明确提及", "NOT_FOUND") and not any(m in cb for m in _EXTRACTION_FAILURE_MARKERS) and not _cb_is_segment_only:
                lines.append(f"**业务简介：** {cb}")
            else:
                lines.append("**业务简介：** *（业务描述暂不可用，10-K文本提取失败）*")
            lines.append("")

            # 收入结构（产品线）
            segs, seg_year, seg_source = _get_revenue_segments(t)
            if segs:
                if seg_year is not None:
                    lines.append(f"**收入结构（产品线，FY{seg_year}）：**")
                else:
                    lines.append("**收入结构（产品线）：**")
                lines.append("")
                for seg in segs[:5]:  # 最多显示5条
                    seg_dict = seg if isinstance(seg, dict) else None
                    # 同时支持SegmentMix对象和dict
                    if hasattr(seg, "segment_name"):
                        name = getattr(seg, "segment_name", None) or ""
                        pct_raw = getattr(seg, "percentage", None)  # 可能是"57.3%"字符串或数字
                        abs_val = getattr(seg, "absolute", None)
                    else:
                        name = (
                            (seg_dict.get("segment") or seg_dict.get("name", ""))
                            if seg_dict is not None
                            else ""
                        )
                        pct_raw = seg_dict.get("percentage") if seg_dict is not None else None
                        abs_val = seg_dict.get("absolute") if seg_dict is not None else None

                    # 处理percentage可能是字符串（"57.3%"）或数字
                    pct = None
                    if pct_raw is not None:
                        try:
                            if isinstance(pct_raw, str):
                                pct = float(pct_raw.replace("%", "").strip())
                            else:
                                pct = float(pct_raw)
                        except (ValueError, TypeError):
                            pass

                    if name:
                        seg_line = f"- {name}"
                        if pct is not None:
                            seg_line += f"：{pct:.1f}%"
                        if abs_val is not None:
                            try:
                                seg_line += f"　(${float(abs_val)/1e9:.2f}B)"
                            except (ValueError, TypeError):
                                pass
                        lines.append(seg_line)
                lines.append("")
                if seg_year is not None and seg_source != "NONE":
                    lines.append(f"*（数据来源：{seg_source}，FY{seg_year}）*")
                    lines.append("")
            else:
                lines.append("**收入结构（产品线）：** *暂无可用分部数据（FMP未收录，10-K提取失败）*")
                lines.append("")

            # 地理收入分布（剔除与产品线同名或明显业务线用词的误填）
            from research_automation.extractors.fmp_client import (
                _normalize_revenue_label,
                _region_label_looks_like_product_line,
            )

            seg_norms: set[str] = set()
            for seg in segs or []:
                if hasattr(seg, "segment_name"):
                    nm = getattr(seg, "segment_name", None) or ""
                else:
                    sd = seg if isinstance(seg, dict) else {}
                    nm = (
                        (sd.get("segment") or sd.get("name", ""))
                        if sd
                        else ""
                    )
                nn = _normalize_revenue_label(str(nm))
                if nn:
                    seg_norms.add(nn)

            geos_filtered: list[Any] = []
            for geo in profile.revenue_by_geography or []:
                if hasattr(geo, "segment_name"):
                    gname = getattr(geo, "segment_name", None) or ""
                elif hasattr(geo, "region_name"):
                    gname = getattr(geo, "region_name", None) or ""
                else:
                    gd = geo if isinstance(geo, dict) else {}
                    gname = (
                        gd.get("region_name")
                        or gd.get("segment_name")
                        or gd.get("region")
                        or gd.get("name", "")
                    )
                if _region_label_looks_like_product_line(str(gname)):
                    continue
                if _normalize_revenue_label(str(gname)) in seg_norms:
                    continue
                geos_filtered.append(geo)

            geos = geos_filtered
            if geos:
                lines.append("**地理分布：**")
                lines.append("")
                for geo in geos[:4]:  # 最多显示4条
                    geo_dict = geo if isinstance(geo, dict) else None
                    region_name = getattr(geo, "region_name", None)
                    if region_name is not None:
                        name = region_name or ""
                        pct_raw = getattr(geo, "percentage", None)
                    elif hasattr(geo, "segment_name"):
                        name = getattr(geo, "segment_name", None) or ""
                        pct_raw = getattr(geo, "percentage", None)
                    else:
                        name = (
                            (geo_dict.get("region") or geo_dict.get("name", ""))
                            if geo_dict is not None
                            else ""
                        )
                        pct_raw = geo_dict.get("percentage") if geo_dict is not None else None

                    pct = None
                    if pct_raw is not None:
                        try:
                            if isinstance(pct_raw, str):
                                pct = float(pct_raw.replace("%", "").strip())
                            else:
                                pct = float(pct_raw)
                        except (ValueError, TypeError):
                            pass

                    if name:
                        geo_line = f"- {name}"
                        if pct is not None:
                            geo_line += f"：{pct:.1f}%"
                        lines.append(geo_line)
                lines.append("")
            else:
                lines.append(
                    "**地理分布：** *暂无地理收入拆分数据（画像中无可用地理项或与产品线重合已过滤）*"
                )
                lines.append("")

        except ProfileGenerationError as e:
            lines.append(f"*（画像生成失败：{e.message}）*")
            lines.append("")
        except Exception:
            logger.exception("H-11 业务卡片失败 ticker=%s", t)
            lines.append("*（数据获取失败，详见日志）*")
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
) -> tuple[str, dict[str, Any]]:
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
        return "# 行业报告（六步结构）\n\n（sector 为空）\n", {
            "sector_quarterly": {},
            "per_company_quarterly": {},
            "sector_name": sec,
        }

    def _cached_report_with_quarterly(cached: str) -> tuple[str, dict[str, Any]]:
        _loaded = _load_per_company_signals_and_insiders(sec, db, thr, report_stats)
        if _loaded:
            _, _per_company, _, _ = _loaded
            _quarterly = _step6_quarterly_charts_section(sec, _per_company)
        else:
            _quarterly = {
                "sector_quarterly": {},
                "per_company_quarterly": {},
                "sector_name": sec,
            }
        return cached, _quarterly

    now_utc = datetime.now(timezone.utc)
    cache_year = now_utc.year
    cache_quarter = (now_utc.month - 1) // 3 + 1
    if cache_quarter == 1:
        cache_quarter = 4
        cache_year -= 1
    else:
        cache_quarter -= 1

    lock = _get_sector_lock(sec)
    if not lock.acquire(blocking=False):
        lock.acquire(blocking=True)
        lock.release()
        if not force_refresh:
            cached = get_cached_report(sec, cache_year, cache_quarter)
            if cached:
                return _cached_report_with_quarterly(cached)
        now_wait = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M")
        return (
            f"# 行业报告（六步结构）：{sec}\n\n"
            f"**生成时间**：{now_wait} UTC\n\n（报告生成中，请稍后刷新）\n",
            {
                "sector_quarterly": {},
                "per_company_quarterly": {},
                "sector_name": sec,
            },
        )

    try:
        if not force_refresh:
            cached = get_cached_report(sec, cache_year, cache_quarter)
            if cached:
                return _cached_report_with_quarterly(cached)

        loaded = _load_per_company_signals_and_insiders(sec, db, thr, report_stats)
        if loaded is None:
            now = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M")
            return (
                f"# 行业报告（六步结构）：{sec}\n\n"
                f"**生成时间**：{now} UTC\n\n未找到该 sector 的活跃公司。\n",
                {
                    "sector_quarterly": {},
                    "per_company_quarterly": {},
                    "sector_name": sec,
                },
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
        from research_automation.core.database import get_step_cache, set_step_cache
        import json as _json

        _STEP_CACHE_VERSION = 6  # 强制清除含残缺sector总结的旧缓存

        def _get_lines_cache(step: str) -> list[str] | None:
            cached = get_step_cache(
                sec,
                cache_year,
                cache_quarter,
                step,
                cache_version=_STEP_CACHE_VERSION,
            )
            if cached:
                try:
                    return _json.loads(cached)
                except Exception:
                    pass
            return None

        def _set_lines_cache(step: str, lines_data: list[str]) -> None:
            try:
                set_step_cache(
                    sec,
                    cache_year,
                    cache_quarter,
                    step,
                    _json.dumps(lines_data, ensure_ascii=False),
                    cache_version=_STEP_CACHE_VERSION,
                )
            except Exception:
                pass

        lines.extend(_step0b_company_snapshot_table(per_company))
        lines.extend(_step2_per_company_revenue_breakdown(per_company))

        # Step3：有缓存直接用，没有才调LLM（空列表视为未命中；force_refresh 跳过 step 缓存）
        step3_lines = _get_lines_cache("step3") if not force_refresh else None
        if not step3_lines:
            logger.info("Step3缓存未命中，重新生成 sector=%s", sec)
            step3_lines = _step3_per_company_outlook(per_company)
            if step3_lines:
                _set_lines_cache("step3", step3_lines)
        else:
            logger.info("Step3命中缓存 sector=%s", sec)

        # Step4：有缓存直接用，没有才调LLM（各公司 earnings 另有独立缓存；force_refresh 跳过本层）
        step4_lines = _get_lines_cache("step4") if not force_refresh else None
        if not step4_lines:
            logger.info("Step4缓存未命中，重新生成 sector=%s", sec)
            step4_lines = _step4_earning_call_section(
                sec,
                earnings_cross_review,
                quarters,
                sector_watch_items,
                per_company,
            )
            if step4_lines:
                _set_lines_cache("step4", step4_lines)
        else:
            logger.info("Step4命中缓存 sector=%s", sec)

        # Step5/6不含LLM，每次重新生成保证新鲜度
        step5_lines = _step5_new_biz_acquisitions_insider(per_company)
        step6_lines = _step6_annual_financial_table(per_company, years=3)
        step6_quarterly_data = _step6_quarterly_charts_section(sec, per_company)

        # 执行摘要：有缓存直接用（空列表视为未命中；force_refresh 跳过；失败时不写缓存以免污染）
        exec_summary_lines_cached = (
            _get_lines_cache("exec_summary") if not force_refresh else None
        )
        if not exec_summary_lines_cached:
            logger.info("执行摘要缓存未命中，重新生成 sector=%s", sec)
            exec_summary_lines = _executive_summary(
                sec, step4_lines, step5_lines, step6_lines, sector_watch_items, per_company
            )
            if exec_summary_lines:
                _set_lines_cache("exec_summary", exec_summary_lines)
        else:
            logger.info("执行摘要命中缓存 sector=%s", sec)
            exec_summary_lines = exec_summary_lines_cached

        overview_lines = _step_overview_sector(sec, per_company)
        company_cards_lines = _step_company_cards(per_company)
        # 执行摘要插入报告最前面（header之后）
        header_lines = lines[:4]  # # 标题、生成时间、新闻窗口、空行
        body_lines = lines[4:]
        lines = (
            header_lines
            + overview_lines
            + company_cards_lines
            + exec_summary_lines
            + body_lines
        )

        lines.extend(step3_lines)
        lines.extend(step4_lines)
        lines.extend(step5_lines)
        lines.extend(step6_lines)
        # ── 缓存写入 ──────────────────────────────────────────────
        report_md = "\n".join(lines).rstrip() + "\n"
        save_report_cache(sec, cache_year, cache_quarter, report_md)
        # 季度图表数据附加到report对象上供前端使用
        return report_md, step6_quarterly_data
    finally:
        lock.release()
