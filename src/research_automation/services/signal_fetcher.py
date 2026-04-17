"""基于 Tavily 的「硬核」新闻信号抓取（裁员 / 内部交易 / 业务变化），过滤卖方评级噪音。"""
from __future__ import annotations

import logging
import re
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Any
from urllib.parse import urlparse

from research_automation.extractors import tavily_client

logger = logging.getLogger(__name__)

# 监控池 ticker → 检索用公司常用名（数据库 ``company_name`` 为空时的兜底，提升 Tavily 命中率）
_POOL_SEARCH_LABELS: dict[str, str] = {
    "CTSH": "Cognizant",
    "AVY": "Avery Dennison",
    "IBM": "IBM",
    "PPG": "PPG Industries",
    "JLL": "Jones Lang LaSalle",
    "ACN": "Accenture",
    "EL": "Estée Lauder",
    "TGT": "Target Corporation",
    "UPS": "United Parcel Service",
    "DG": "Dollar General",
    "HCA": "HCA Healthcare",
    "BAH": "Booz Allen Hamilton",
    "MDB": "MongoDB",
    "ZM": "Zoom",
    "RTO": "Rentokil Initial",
    "BT/A LN": "BT Group",
    "FRE GY": "Fresenius",
    "KBX GY": "Knorr-Bremse",
    "DHL GY": "Deutsche Post DHL",
}

# 与 AI 就业替代 / 数字化用工主题强相关的标的（用于相关性 +1，压低纯消费等弱相关噪音）
_STRONG_THEME_TICKERS: frozenset[str] = frozenset(
    {
        "IBM",
        "ACN",
        "CTSH",
        "AVY",
        "PPG",
        "JLL",
        "BAH",
        "MDB",
        "ZM",
        "BT/A LN",
        "FRE GY",
        "KBX GY",
        "DHL GY",
        "RTO",
    }
)

# 一线财经域名（相关性 +1）
_PREMIUM_NEWS_HOST_SUFFIXES: tuple[str, ...] = (
    "bloomberg.com",
    "reuters.com",
    "wsj.com",
    "ft.com",
)


def company_display_name(ticker: str, company_name: str | None = None) -> str:
    """展示用公司名：优先数据库 ``company_name``，否则监控池常用名，再退回 ticker。"""
    sym = (ticker or "").strip()
    n = (company_name or "").strip()
    if n:
        return n
    return _POOL_SEARCH_LABELS.get(sym.upper(), _POOL_SEARCH_LABELS.get(sym, sym))


# 分析师 / 评级类噪音（标题或摘要命中即剔除，大小写不敏感）
_ANALYST_PATTERNS: tuple[re.Pattern[str], ...] = tuple(
    re.compile(p, re.IGNORECASE)
    for p in (
        r"\banalyst\b",
        r"\banalysts\b",
        r"\brating\b",
        r"price\s*target",
        r"\bupgrade\b",
        r"\bdowngrade\b",
        r"\boutperform\b",
        r"\bunderperform\b",
        r"buy\s*rating",
        r"sell\s*rating",
        r"(?:quarterly|revenue|earnings).{0,48}estimates",
    )
)

_LAYOFF_RE = re.compile(
    r"layoff|layoffs|reduction\s+in\s+force|\brif\b|job\s+cut|jobs?\s+were\s+cut|"
    r"headcount\s+reduction|workforce\s+reduction|staff\s+cut|job\s+loss",
    re.IGNORECASE,
)
_INSIDER_RE = re.compile(
    r"insider\s+trading|insider\s+buy|insider\s+sell|form\s+4|"
    r"chief\s+executive.*(?:buy|sell|purchase|sale)",
    re.IGNORECASE,
)
_BUSINESS_RE = re.compile(
    r"\bacquisition\b|\bmerger\b|partnership|\bpartners\b|new\s+product|new\s+service|strategic\s+"
    r"(?:alliance|investment|stake)|\binvest(?:s|ed|ing|ment)\b|takeover|buyout|"
    r"joint\s+venture",
    re.IGNORECASE,
)

# 相关性关键词：标题或摘要命中即 +1（大小写不敏感，部分用整词边界）
_RELEVANCE_KEYWORD_RES: tuple[re.Pattern[str], ...] = tuple(
    re.compile(p, re.IGNORECASE)
    for p in (
        r"\bai\b",
        r"\bautomation\b",
        r"digital\s+transformation",
        r"\bagent\b",
        r"\bllm\b",
        r"layoff",
        r"reduction\s+in\s+force",
        r"job\s+cut",
        r"headcount",
        r"insider\s+trading",
        r"insider\s+buy",
        r"insider\s+sell",
    )
)

# 股价区间 / 行情页等无意义模式（标题+摘要联合判断）
_NOISE_STOCK_RANGE_RE = re.compile(
    r"\$\s*\d[\d,]*(?:\.\d{2})?\s*[-–—]\s*\$\s*\d[\d,]*(?:\.\d{2})?",
    re.IGNORECASE,
)
_NOISE_LITERAL_SUBSTRINGS: tuple[str, ...] = (
    "real-time stock quotes",
    "stock price",
    "download pdf",
    "this copy is for your personal",
    "historical stock prices",
    "stock quote",
    "share price",
)


def _blob(row: dict[str, Any]) -> str:
    return f"{row.get('title') or ''} {row.get('content') or ''}"


def _text_mentions_ticker(text: str, ticker: str) -> bool:
    """标题+摘要中须能关联到标的（整词或完整代码子串），抑制泛化 OR 查询带来的无关条目。"""
    sym = (ticker or "").strip()
    if not sym:
        return False
    blob = (text or "").strip()
    if not blob:
        return False
    if "/" in sym or " " in sym or "." in sym:
        if sym.upper() in blob.upper():
            return True
        compact = re.sub(r"\s+", "", sym.upper())
        if len(compact) >= 3 and compact in re.sub(r"\s+", "", blob.upper()):
            return True
        return False
    return bool(
        re.search(rf"(?<![A-Z0-9]){re.escape(sym.upper())}(?![A-Z0-9])", blob, re.IGNORECASE)
    )


def _row_relevant_to_ticker(
    row: dict[str, Any], ticker: str, company_label: str | None
) -> bool:
    b = _blob(row)
    u = str(row.get("url") or "")
    if _text_mentions_ticker(b, ticker) or _text_mentions_ticker(u, ticker):
        return True
    lab = (company_label or "").strip()
    if len(lab) >= 4 and lab.lower() in (b + " " + u).lower():
        return True
    return False


def _is_generic_listing_url(url: str) -> bool:
    """Reuters/FT 等公司聚合页、 tearsheet 流，易夹带同业噪声。"""
    u = (url or "").lower()
    if "reuters.com/company/" in u:
        return True
    if "markets.ft.com/data/equities/tearsheet" in u:
        return True
    if "ft.com/stream/" in u:
        return True
    return False


def _is_analyst_noise(row: dict[str, Any]) -> bool:
    b = _blob(row)
    u = str(row.get("url") or "")
    if any(p.search(b) for p in _ANALYST_PATTERNS):
        return True
    if "/insights/earnings" in u.lower():
        return True
    if re.search(
        r"earnings\s+(summary|review|insights)|key\s+takeaways|q[1-4]\s+earnings\s+",
        b,
        re.IGNORECASE,
    ):
        return True
    return False


def _is_noise(snippet: str, title: str) -> bool:
    """
    无意义内容：股价区间列表、行情页免责声明、PDF 下载引导等。
    用于过滤 HCA 类「仅展示报价」条目。
    """
    blob = f"{title or ''} {snippet or ''}"
    if not blob.strip():
        return False
    low = blob.lower()
    if _NOISE_STOCK_RANGE_RE.search(blob):
        return True
    for s in _NOISE_LITERAL_SUBSTRINGS:
        if s in low:
            return True
    return False


def _parse_published_to_utc_date(pub: str | None) -> datetime | None:
    """
    将 Tavily ``published_date`` 解析为 UTC 的 datetime（取日历日用于窗口比较）。
    解析失败返回 ``None``（调用方保留条目并打日志）。
    """
    if pub is None:
        return None
    raw = str(pub).strip()
    if not raw:
        return None
    # 常见：2024-01-15、2024-01-15T12:00:00、带 Z
    raw = raw.replace("Z", "+00:00")
    try:
        if len(raw) >= 10 and raw[4] == "-" and raw[7] == "-":
            if "T" in raw or "+" in raw or raw.count("-") > 2:
                dt = datetime.fromisoformat(raw[:32])
            else:
                dt = datetime.strptime(raw[:10], "%Y-%m-%d")
            if dt.tzinfo is None:
                dt = dt.replace(tzinfo=timezone.utc)
            return dt.astimezone(timezone.utc)
    except ValueError:
        pass
    try:
        return datetime.strptime(raw[:10], "%Y-%m-%d").replace(tzinfo=timezone.utc)
    except ValueError:
        return None


def _published_within_window(
    published_date: str | None, days_back: int, *, ticker: str, url: str
) -> tuple[bool, bool]:
    """
    返回 ``(是否保留, 是否因日期明确过期而丢弃)``。
    解析失败：保留，``False``；解析成功且早于窗口：丢弃，``True``。
    """
    now = datetime.now(timezone.utc)
    cutoff = now - timedelta(days=max(1, int(days_back)))
    dt = _parse_published_to_utc_date(published_date)
    if dt is None:
        logger.debug(
            "Tavily 信号 published_date 无法解析，保留条目 ticker=%s url=%s raw=%r",
            ticker,
            (url or "")[:120],
            published_date,
        )
        return True, False
    if dt < cutoff:
        return False, True
    return True, False


def _host_for_url(url: str) -> str:
    try:
        return (urlparse(url).hostname or "").lower()
    except ValueError:
        return ""


def _relevance_premium_domain(url: str) -> bool:
    host = _host_for_url(url)
    return any(host == s or host.endswith("." + s) for s in _PREMIUM_NEWS_HOST_SUFFIXES)


def _relevance_keyword_hits(blob: str) -> bool:
    return any(p.search(blob) for p in _RELEVANCE_KEYWORD_RES)


def compute_relevance_score(row: dict[str, Any], ticker: str) -> int:
    """
    整数 0–3：关键词命中、强主题标的、一线域名各最多 +1。
    供报告侧按阈值过滤弱相关（如 Estée Lauder 纯美妆无关稿）。
    """
    sym_u = (ticker or "").strip().upper()
    blob = _blob(row)
    url = str(row.get("url") or "")
    score = 0
    if _relevance_keyword_hits(blob):
        score += 1
    if sym_u in _STRONG_THEME_TICKERS or sym_u.replace(" ", "") in _STRONG_THEME_TICKERS:
        score += 1
    if _relevance_premium_domain(url):
        score += 1
    return min(3, score)


def _classify_signal(row: dict[str, Any]) -> str:
    b = _blob(row)
    if _LAYOFF_RE.search(b):
        return "layoff"
    if _INSIDER_RE.search(b):
        return "insider_trade"
    if _BUSINESS_RE.search(b):
        return "business_change"
    return "other"


def _queries_for_ticker(ticker: str, company_label: str | None) -> list[tuple[str, str]]:
    """(query_id, full_query_string) — 短句 + 引号公司名；``topic=finance`` 与域名白名单配合更稳。"""
    sym = (ticker or "").strip()
    lab = (company_label or "").strip()
    if lab and lab.strip().upper() != sym.strip().upper():
        qn = lab.replace('"', '\\"')
        return [
            (
                "layoff",
                f'"{qn}" (layoff OR "job cut" OR headcount OR workforce OR RIF)',
            ),
            (
                "insider",
                f'"{qn}" ("insider trading" OR "insider buy" OR "insider sell" OR "Form 4") '
                f"OR ({sym} insider)",
            ),
            (
                "business",
                f'"{qn}" (acquisition OR merger OR partnership OR "new product" OR '
                f'"strategic alliance" OR takeover)',
            ),
        ]
    return [
        ("layoff", f"{sym} (layoff OR \"job cut\" OR headcount)"),
        ("insider", f'{sym} ("insider trading" OR "Form 4")'),
        (
            "business",
            f"{sym} (acquisition OR partnership OR merger OR investment)",
        ),
    ]


@dataclass
class SignalFetchStats:
    """单次 ``fetch_signals_for_ticker`` 的统计，供报告调试区块汇总。"""

    raw_row_count: int = 0  # Tavily 返回的原始条数（含跨查询重复 URL）
    unique_url_count: int = 0  # 按 url 合并后的条数
    dropped_duplicate_rows: int = 0  # raw_row_count - unique_url_count（近似跨查询重复）
    dropped_expired: int = 0  # 解析到日期且超出 days_back（UTC）
    dropped_noise: int = 0  # _is_noise 等无意义内容
    dropped_other: int = 0  # 分析师噪声、泛化页、标的弱匹配等（不含过期与行情噪音）

    def merge_from(self, other: SignalFetchStats) -> None:
        self.raw_row_count += other.raw_row_count
        self.unique_url_count += other.unique_url_count
        self.dropped_duplicate_rows += other.dropped_duplicate_rows
        self.dropped_expired += other.dropped_expired
        self.dropped_noise += other.dropped_noise
        self.dropped_other += other.dropped_other


def fetch_signals_for_ticker(
    ticker: str,
    days_back: int = 7,
    *,
    company_name: str | None = None,
    max_results_per_query: int = 10,
    stats_out: SignalFetchStats | None = None,
) -> list[dict[str, Any]]:
    """
    对单标的执行多查询 Tavily 搜索，按 ``url`` 去重，UTC 窗口内 ``published_date`` 严格过滤，
    剔除分析师评级与无意义行情噪音，并打上 ``signal_type``、``relevance_score``（0–3）。

    ``company_name`` 非空时优先用于查询与相关性；否则使用内置池内常用名兜底。

    ``stats_out`` 若传入则累加本函数内的原始条数、去重、过期、噪音剔除计数。
    """
    acc = SignalFetchStats()

    sym = (ticker or "").strip()
    if not sym:
        return []

    sym_u = sym.upper()
    company_label = (company_name or "").strip() or _POOL_SEARCH_LABELS.get(
        sym_u, _POOL_SEARCH_LABELS.get(sym, "")
    )

    by_url: dict[str, dict[str, Any]] = {}
    url_axes: dict[str, set[str]] = {}
    raw_row_count = 0

    for axis, qtext in _queries_for_ticker(sym, company_label or None):
        rows = tavily_client.search_news(
            qtext,
            days_back=days_back,
            max_results=max_results_per_query,
            topic="finance",
        )
        for row in rows:
            raw_row_count += 1
            url = (row.get("url") or "").strip()
            if not url:
                continue
            key = url.lower()
            if key not in by_url:
                by_url[key] = {
                    "title": row.get("title") or "",
                    "url": url,
                    "content": row.get("content") or "",
                    "published_date": row.get("published_date"),
                }
                url_axes[key] = set()
            url_axes[key].add(axis)

    acc.raw_row_count += raw_row_count
    acc.unique_url_count += len(by_url)
    acc.dropped_duplicate_rows += max(0, raw_row_count - len(by_url))

    merged: list[dict[str, Any]] = []
    for key, row in by_url.items():
        url = str(row.get("url") or "")
        title = str(row.get("title") or "")
        content = str(row.get("content") or "")
        if _is_generic_listing_url(url):
            acc.dropped_other += 1
            continue
        if not _row_relevant_to_ticker(row, sym, company_label or None):
            acc.dropped_other += 1
            continue
        if _is_analyst_noise(row):
            acc.dropped_other += 1
            continue
        if _is_noise(content, title):
            acc.dropped_noise += 1
            continue

        keep, expired = _published_within_window(
            row.get("published_date"), days_back, ticker=sym, url=url
        )
        if expired:
            acc.dropped_expired += 1
        if not keep:
            continue

        axes = url_axes.get(key) or set()
        row = dict(row)
        row["query_axis"] = ",".join(sorted(axes))
        row["signal_type"] = _classify_signal(row)
        row["relevance_score"] = int(compute_relevance_score(row, sym))
        merged.append(row)

    merged.sort(
        key=lambda r: (
            -int(r.get("relevance_score") or 0),
            str(r.get("published_date") or ""),
            str(r.get("title") or ""),
        )
    )

    if stats_out is not None:
        stats_out.merge_from(acc)

    return merged
