"""
SEC EDGAR：按 ticker / 年份拉取 10-K 并解析「Item 1. Business」纯文本。

SEC 要求请求须带可识别应用的 User-Agent（含联系方式），见：
https://www.sec.gov/os/accessing-edgar-data
"""
from __future__ import annotations

import json
import os
import re
import ssl
import time
import warnings
import urllib.error
import urllib.request
from io import StringIO
from pathlib import Path
from typing import Any

import pandas as pd
from bs4 import BeautifulSoup, XMLParsedAsHTMLWarning

from research_automation.models.financial import AnnualFinancials

# --- 路径与网络 ---


def _project_root() -> Path:
    # sec_edgar.py -> extractors -> research_automation -> src -> 项目根
    return Path(__file__).resolve().parents[3]


def _user_agent() -> str:
    """User-Agent；勿使用通用浏览器串，应能通过环境变量覆盖。"""
    default = "AIResearchAutomation/1.0 (research-poc@localhost; contact in README)"
    return (os.environ.get("SEC_EDGAR_USER_AGENT") or default).strip() or default


def _sec_request(url: str) -> bytes:
    """发起 GET；429/5xx 抛 SecEdgarError。"""
    hdrs = {
        "User-Agent": _user_agent(),
        "Accept": "application/json,text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
        "Accept-Language": "en-US,en;q=0.9",
    }
    # Archives 站点常校验 Referer，缺省易返回 403
    if "://www.sec.gov/" in url:
        hdrs["Referer"] = "https://www.sec.gov/"
    req = urllib.request.Request(
        url,
        headers=hdrs,
        method="GET",
    )
    ssl_ctx: ssl.SSLContext | None = None
    try:
        import certifi

        ssl_ctx = ssl.create_default_context(cafile=certifi.where())
    except ImportError:
        ssl_ctx = None
    try:
        kw: dict[str, Any] = {"timeout": 90}
        if ssl_ctx is not None:
            kw["context"] = ssl_ctx
        with urllib.request.urlopen(req, **kw) as resp:
            return resp.read()
    except urllib.error.HTTPError as e:
        raise SecEdgarError(f"SEC HTTP {e.code}：{url}") from e
    except urllib.error.URLError as e:
        raise SecEdgarError(f"SEC 网络错误：{e.reason!s} ({url})") from e


# 两次 SEC 请求之间稍作间隔，降低封禁风险
_LAST_SEC_REQ_MONO: float = 0.0


def _throttled_sec_get(url: str) -> bytes:
    global _LAST_SEC_REQ_MONO

    gap = 0.2
    now = time.monotonic()
    wait = gap - (now - _LAST_SEC_REQ_MONO)
    if wait > 0:
        time.sleep(wait)
    out = _sec_request(url)
    _LAST_SEC_REQ_MONO = time.monotonic()
    return out


class SecEdgarError(Exception):
    """无法从 EDGAR 获取或解析 10-K / Item 1。"""


# --- CIK：预置 + 官方 company_tickers 缓存 ---

# 常用标的（offline / 拉取失败时兜底）；数值与 SEC 一致
_TICKER_CIK_PRELOAD: dict[str, int] = {
    "AAPL": 320193,
    "MSFT": 789019,
    "GOOGL": 1652044,
    "GOOG": 1652044,
    "AMZN": 1018724,
    "META": 1326801,
    "NVDA": 1045810,
    "TSLA": 1318605,
    "JPM": 19617,
    "V": 1403161,
}


def _company_tickers_path() -> Path:
    return _project_root() / "data" / "raw" / "sec" / "company_tickers.json"


def _load_ticker_to_cik() -> dict[str, int]:
    """合并预置表与 SEC ``company_tickers.json``（下载并缓存）。"""
    out: dict[str, int] = {k.upper(): v for k, v in _TICKER_CIK_PRELOAD.items()}
    path = _company_tickers_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    if not path.exists() or path.stat().st_size == 0:
        data = _throttled_sec_get("https://www.sec.gov/files/company_tickers.json")
        path.write_bytes(data)
    raw_obj = json.loads(path.read_text(encoding="utf-8"))
    rows: list[Any]
    if isinstance(raw_obj, dict):
        rows = list(raw_obj.values())
    elif isinstance(raw_obj, list):
        rows = raw_obj
    else:
        rows = []
    for row in rows:
        if not isinstance(row, dict):
            continue
        t = row.get("ticker")
        cik = row.get("cik_str")
        if t is None or cik is None:
            continue
        sym = str(t).strip().upper()
        out[sym] = int(cik)
    return out


def get_cik(ticker: str) -> int:
    """根据 ticker 解析 CIK（整数，无前导零）。"""
    sym = (ticker or "").strip().upper()
    if not sym:
        raise SecEdgarError("ticker 不能为空")
    if sym in _TICKER_CIK_PRELOAD:
        return _TICKER_CIK_PRELOAD[sym]
    m = _load_ticker_to_cik()
    if sym not in m:
        raise SecEdgarError(f"未在 SEC company_tickers 中找到 ticker：{sym}")
    return m[sym]


def _cik_padded_10(cik: int) -> str:
    return f"{cik:010d}"


def _fetch_submissions(cik: int) -> dict[str, Any]:
    url = f"https://data.sec.gov/submissions/CIK{_cik_padded_10(cik)}.json"
    raw = _throttled_sec_get(url)
    return json.loads(raw.decode("utf-8"))


def _find_10k_filing(
    submissions: dict[str, Any], filing_year: int
) -> tuple[str, str] | None:
    """
    在 recent filings 中寻找 ``form == 10-K`` 且 filingDate 落在 ``filing_year`` 公历年。
    返回 (accessionNumber, primaryDocument)。
    """
    recent = submissions.get("filings", {}).get("recent")
    if not isinstance(recent, dict):
        return None
    forms = recent.get("form", [])
    dates = recent.get("filingDate", [])
    accs = recent.get("accessionNumber", [])
    docs = recent.get("primaryDocument", [])
    n = min(len(forms), len(dates), len(accs), len(docs))
    target_prefix = str(filing_year)
    for i in range(n):
        if str(forms[i]).upper() not in ("10-K", "10-K/A"):
            continue
        fd = str(dates[i])[:4] if dates[i] else ""
        if fd == target_prefix:
            return str(accs[i]), str(docs[i])
    return None


def _resolve_10k_filing(
    submissions: dict[str, Any], year: int
) -> tuple[str, str]:
    """按 ``year`` 查找，若无则依次尝试 ``year-1``、``year-2``。"""
    for y in (year, year - 1, year - 2):
        hit = _find_10k_filing(submissions, y)
        if hit is not None:
            return hit
    raise SecEdgarError(f"未找到 {year} 年前后可匹配的 10-K 申报（form=10-K）。")


def _filing_url(cik: int, accession: str, primary_document: str) -> str:
    acc_dir = accession.replace("-", "")
    return (
        f"https://www.sec.gov/Archives/edgar/data/{cik}/{acc_dir}/{primary_document}"
    )


def _fetch_primary_document(cik: int, accession: str, primary_document: str) -> str:
    url = _filing_url(cik, accession, primary_document)
    return _throttled_sec_get(url).decode("utf-8", errors="replace")


def _html_to_plain_text(html: str) -> str:
    """10-K 主文档 HTML → 纯文本（与 Item 1 解析共用）。"""
    with warnings.catch_warnings():
        warnings.simplefilter("ignore", XMLParsedAsHTMLWarning)
        soup = BeautifulSoup(html, "lxml")
    text = soup.get_text("\n")
    text = re.sub(r"[\xa0\t\f\r]+", " ", text)
    text = re.sub(r"\n{3,}", "\n\n", text)
    return text


def _best_chunk_from_starts(
    text: str,
    start_pat: re.Pattern[str],
    end_pats: list[re.Pattern[str]],
    *,
    min_scan: int = 80,
) -> str:
    """
    从 ``start_pat`` 的多个匹配中取**最长**一段（缓解目录 TOC 短匹配）；
    在 ``end_pats`` 中任一则截断（取最早出现者）。
    """
    candidates = list(start_pat.finditer(text))
    if not candidates:
        return ""

    def slice_at_ends(start_idx: int) -> str:
        body = text[start_idx:]
        cut = len(body)
        for ep in end_pats:
            m = ep.search(body, min_scan)
            if m is not None and m.start() < cut:
                cut = m.start()
        return body[:cut].strip()

    best = ""
    for m in reversed(candidates):
        chunk = slice_at_ends(m.start())
        if len(chunk) > len(best):
            best = chunk
        if len(chunk) > 800:
            break
    if best:
        return best
    return slice_at_ends(candidates[0].start())


# 章节边界（10-K 常见标题；不同发行人措辞略有差异）
_RE_ITEM1A = re.compile(r"(?is)\bitem\s*1a\.?\s*risk\s*factors\b")
_RE_ITEM1B = re.compile(r"(?is)\bitem\s*1b\.?\s*")
_RE_ITEM2 = re.compile(r"(?is)\bitem\s*2\.?\s*properties\b")
# 真 MD&A 标题（避免匹配「Item 7 of the … Annual Report」类交叉引用及 Item 7A）
_RE_ITEM7 = re.compile(
    r"(?is)\bitem\s*7\.?\s*"
    r"(?:management['\u2019]?\s*s\s+discussion|"
    r"discussion\s+and\s+analysis|"
    r"md\s*&\s*a\b)"
)
_RE_ITEM7_FALLBACK = re.compile(r"(?is)\bitem\s*7\.?\s*(?!a\b)")
_RE_ITEM7A = re.compile(r"(?is)\bitem\s*7a\.?\s*")
_RE_ITEM8 = re.compile(
    r"(?is)\bitem\s*8\.?\s*(?:financial\s*statements|consolidated\s*financial|"
    r"index\s*to\s*consolidated\s*financial)\b"
)
_RE_ITEM8_LOOSE = re.compile(r"(?is)\bitem\s*8\.?\s*")
# 作為「新章节」的 Item 8 标题（避免正文「见 Item 8」误截断 MD&A）
_RE_ITEM8_SECTION_START = re.compile(
    r"(?is)(?:\n|\r\n)\s*item\s*8\.?\s*(?:financial|statements|supplementary)\b"
)
_RE_ITEM9 = re.compile(r"(?is)\bitem\s*9\.?\s*")


def _extract_item1_business_from_plain(text: str) -> str:
    """纯文本上截取 Item 1 Business（至 Item 1A 或 Item 2）。"""
    pat_start = re.compile(r"(?is)\bitem\s*1\.?\s*business\b")
    chunk = _best_chunk_from_starts(text, pat_start, [_RE_ITEM1A, _RE_ITEM2])
    if chunk:
        return chunk
    return ""


def _extract_item1a_from_plain(text: str) -> str:
    return _best_chunk_from_starts(text, _RE_ITEM1A, [_RE_ITEM1B, _RE_ITEM2])


def _extract_item7_from_plain(text: str) -> str:
    # 正文常出现「参见 Item 8」交叉引用，不得以宽松 ``Item 8`` 字串作结束边界
    chunk = _best_chunk_from_starts(text, _RE_ITEM7, [_RE_ITEM7A, _RE_ITEM8_SECTION_START])
    if len(chunk) > 400:
        return chunk
    # 回退：部分版式在「Item 7」与标题间换行，用宽松起点并剔除明显交叉引用段
    loose_matches = list(_RE_ITEM7_FALLBACK.finditer(text))
    best = chunk
    for m in reversed(loose_matches):
        head = text[m.start() : m.start() + 220]
        if re.search(
            r"(?is)annual\s+report\s+on\s+form\s+10[- ]?k|"
            r"cross[- ]?reference|incorporated\s+by\s+reference",
            head,
        ):
            continue
        slice_rest = text[m.start() :]
        cut = len(slice_rest)
        for ep in (_RE_ITEM7A, _RE_ITEM8_SECTION_START):
            em = ep.search(slice_rest, 80)
            if em is not None and em.start() < cut:
                cut = em.start()
        cand = slice_rest[:cut].strip()
        if len(cand) > len(best) and re.search(
            r"(?is)(net\s+sales|revenue|operating\s+income|results\s+of\s+operations)",
            cand[:8000],
        ):
            best = cand
        if len(best) > 2000:
            break
    return best


def _extract_item8_full_from_plain(text: str) -> str:
    chunk = _best_chunk_from_starts(text, _RE_ITEM8, [_RE_ITEM9])
    if len(chunk) > 200:
        return chunk
    return _best_chunk_from_starts(text, _RE_ITEM8_LOOSE, [_RE_ITEM9])


_SEGMENT_ANCHORS = (
    "segment information",
    "disaggregation of revenue",
    "disaggregation of net sales",
    "reportable segment",
    "operating segments",
    "products and services",
    "geographic information",
    "note 16",
    "note 17",
    "note 18",
    "net sales by",
    "revenue by segment",
)


def _narrow_item8_to_segment_notes(item8_text: str, max_chars: int = 55_000) -> str:
    """
    Item 8 全文极长：优先截取含分部/地区营收附注的窗口，便于 LLM 区分产品线 vs 地理占比。
    """
    t = (item8_text or "").strip()
    if not t:
        return ""
    if len(t) <= max_chars:
        return t
    low = t.lower()
    positions = [low.find(a) for a in _SEGMENT_ANCHORS]
    positions = [p for p in positions if p >= 0]
    if not positions:
        return t[:max_chars]
    pos = min(positions)
    half = max_chars // 2
    start = max(0, pos - half)
    end = min(len(t), start + max_chars)
    start = max(0, end - max_chars)
    return t[start:end].strip()


def _extract_item1_business(html: str) -> str:
    """
    从 10-K 主文档 HTML 中截取 Item 1 Business 至 Item 1A Risk Factors（不含）之间的文本。
    目录中常出现简短「Item 1. Business」链接，故优先取**末尾**匹配段且长度足够者。
    """
    text = _html_to_plain_text(html.replace("\r\n", "\n"))
    return _extract_item1_business_from_plain(text)


def _cache_path(ticker: str, year: int) -> Path:
    sym = (ticker or "").strip().upper()
    root = _project_root() / "data" / "raw" / "10k"
    root.mkdir(parents=True, exist_ok=True)
    return root / f"{sym}_{year}.txt"


def _cache_path_full_html(ticker: str, year: int) -> Path:
    """完整 10-K 主文档 HTML 缓存路径（供 Item 8 表格解析复用）。"""
    sym = (ticker or "").strip().upper()
    root = _project_root() / "data" / "raw" / "10k"
    root.mkdir(parents=True, exist_ok=True)
    return root / f"{sym}_{year}_full.html"


def _cache_path_section(sym: str, year: int, section_key: str) -> Path:
    """各章节纯文本缓存：``item1`` / ``item1a`` / ``item7`` / ``item8_notes``。"""
    root = _project_root() / "data" / "raw" / "10k"
    root.mkdir(parents=True, exist_ok=True)
    return root / f"{sym}_{year}_sec_{section_key}.txt"


def get_10k_sections(ticker: str, year: int) -> dict[str, str]:
    """
    拉取指定公历年附近 10-K，解析并缓存各章节纯文本。

    返回::

        {
            "item1": "...",
            "item1a": "...",
            "item7": "...",
            "item8_notes": "...",  # Item 8 中侧重分部/地区营收附注的节选
        }

    解析主文档 HTML/XML，章节边界依赖常见「Item N」标题；失败章节为空串。
    与 ``get_10k_text`` / ``_get_10k_raw_html`` 共用 ``*_full.html`` 下载缓存。
    """
    sym = (ticker or "").strip().upper()
    if not sym:
        raise SecEdgarError("ticker 不能为空")

    keys = ("item1", "item1a", "item7", "item8_notes")
    paths = {k: _cache_path_section(sym, year, k) for k in keys}
    if all(p.exists() and p.stat().st_size > 0 for p in paths.values()):
        return {k: paths[k].read_text(encoding="utf-8", errors="replace") for k in keys}

    raw_doc = _get_10k_raw_html(sym, year)
    plain = _html_to_plain_text(raw_doc.replace("\r\n", "\n"))

    item1 = _extract_item1_business_from_plain(plain)
    if not item1 or len(item1.strip()) < 80:
        legacy = _cache_path(sym, year)
        if legacy.exists() and legacy.stat().st_size > 0:
            item1 = legacy.read_text(encoding="utf-8", errors="replace")

    item1a = _extract_item1a_from_plain(plain)
    item7 = _extract_item7_from_plain(plain)
    item8_full = _extract_item8_full_from_plain(plain)
    item8_notes = _narrow_item8_to_segment_notes(item8_full)

    out = {
        "item1": item1.strip(),
        "item1a": item1a.strip(),
        "item7": item7.strip(),
        "item8_notes": item8_notes.strip(),
    }
    for k in keys:
        paths[k].write_text(out[k], encoding="utf-8")

    legacy_item1 = _cache_path(sym, year)
    if out["item1"] and (not legacy_item1.exists() or legacy_item1.stat().st_size == 0):
        legacy_item1.write_text(out["item1"], encoding="utf-8")

    return out


def _get_10k_raw_html(ticker: str, year: int) -> str:
    """
    获取指定「申报公历年」对应 10-K 主文档的原始 HTML；优先读缓存 ``*_full.html``。
    与 ``get_10k_text`` 共用同一次下载，避免重复请求 SEC。
    """
    sym = (ticker or "").strip().upper()
    if not sym:
        raise SecEdgarError("ticker 不能为空")

    cache_html = _cache_path_full_html(sym, year)
    if cache_html.exists() and cache_html.stat().st_size > 0:
        return cache_html.read_text(encoding="utf-8", errors="replace")

    cik = get_cik(sym)
    subs = _fetch_submissions(cik)
    accession, primary_doc = _resolve_10k_filing(subs, year)
    if not primary_doc:
        raise SecEdgarError("SEC submissions 中缺少 primaryDocument。")

    raw_doc = _fetch_primary_document(cik, accession, primary_doc)
    cache_html.write_text(raw_doc, encoding="utf-8")
    return raw_doc


def get_10k_text(ticker: str, year: int) -> str:
    """
    拉取指定公历年附近提交的 10-K，解析「Item 1. Business」为纯文本。

    - 使用 CIK 映射（预置 + SEC ``company_tickers.json`` 缓存）。
    - 与 ``get_10k_sections`` 共享章节缓存；遗留路径 ``{TICKER}_{year}.txt`` 仍会写入 Item 1。
    """
    sym = (ticker or "").strip().upper()
    if not sym:
        raise SecEdgarError("ticker 不能为空")

    cache = _cache_path(sym, year)
    if cache.exists() and cache.stat().st_size > 0:
        return cache.read_text(encoding="utf-8", errors="replace")

    sections = get_10k_sections(sym, year)
    item1 = (sections.get("item1") or "").strip()

    if not item1 or len(item1) < 100:
        raise SecEdgarError(
            "未能从 10-K 主文档中解析出足够长度的 Item 1 Business（请检查表格版式或招股书类型）。"
        )

    cache.write_text(item1, encoding="utf-8")
    return item1


# --- Item 8：10-K 合并报表 HTML 表格解析（pandas.read_html） ---

_YEAR_IN_CELL = re.compile(r"\b(20[0-3]\d)\b")


def _year_from_cell(s: str) -> int | None:
    """从表头单元格（如 September 30, 2023）提取四位财年。"""
    m = _YEAR_IN_CELL.search(s)
    if not m:
        return None
    y = int(m.group(1))
    if 2000 <= y <= 2035:
        return y
    return None


def _parse_money_cell(val: Any) -> float | None:
    """将报表单元格转为 float；括号表示负数。"""
    if val is None:
        return None
    if isinstance(val, float) and pd.isna(val):
        return None
    if isinstance(val, (int, float)):
        return float(val)
    s = str(val).strip()
    if not s or s in ("—", "-", "–", "nan"):
        return None
    neg = False
    if s.startswith("(") and s.endswith(")"):
        neg = True
        s = s[1:-1]
    s = s.replace("$", "").replace(",", "").replace(" ", "").strip()
    try:
        x = float(s)
    except ValueError:
        return None
    return -abs(x) if neg else x


def _row_label(df: pd.DataFrame, r: int) -> str:
    """合并前两列作为行描述（适配 ix 双列标签）。"""
    parts: list[str] = []
    for c in range(min(2, df.shape[1])):
        v = df.iloc[r, c]
        if pd.notna(v):
            parts.append(str(v).strip().lower())
    return " ".join(parts)


def _table_text_blob(df: pd.DataFrame, max_label_cols: int = 3) -> str:
    """将表内前几列文本拼成一大串，用于判断是否为利润表/现金流/资产负债表。"""
    parts: list[str] = []
    nc = min(max_label_cols, df.shape[1])
    for c in range(nc):
        for x in df.iloc[:, c]:
            if pd.notna(x):
                parts.append(str(x).lower())
    return " ".join(parts)


def _pick_statement_tables(
    tables: list[pd.DataFrame],
) -> tuple[pd.DataFrame | None, pd.DataFrame | None, pd.DataFrame | None]:
    """
    在全文档表格中识别三张合并报表（先匹配者胜出，适配 AAPL / MSFT 等常见 ix 版式）。
    """
    income: pd.DataFrame | None = None
    cashflow: pd.DataFrame | None = None
    balance: pd.DataFrame | None = None

    for t in tables:
        if t.shape[0] < 4 or t.shape[1] < 4:
            continue
        blob = _table_text_blob(t)
        if income is None:
            if (
                ("total net sales" in blob or "total revenue" in blob)
                and ("gross margin" in blob or "gross profit" in blob)
                and "operating income" in blob
            ):
                income = t
                continue
        if balance is None:
            if (
                "total assets" in blob
                and "total liabilities" in blob
                and "shareholders" in blob
            ):
                balance = t
                continue
        if cashflow is None:
            if "cash generated by operating activities" in blob or (
                "operating activities" in blob
                and "investing activities" in blob
                and "financing activities" in blob
            ):
                cashflow = t
                continue

    return income, cashflow, balance


def _column_groups_by_fiscal_year(df: pd.DataFrame) -> dict[int, list[int]]:
    """
    扫描表头前几行，构建「财年 -> 列索引列表」（同一财年常对应多列重复数值列）。
    """
    for r in range(min(6, len(df))):
        row_map: dict[int, list[int]] = {}
        for c in range(df.shape[1]):
            cell = df.iloc[r, c]
            if pd.isna(cell):
                continue
            y = _year_from_cell(str(cell))
            if y is None:
                continue
            row_map.setdefault(y, []).append(int(c))
        if len(row_map) >= 2:
            return row_map
    return {}


def _resolve_fiscal_year_for_columns(
    col_groups: dict[int, list[int]], filing_year: int
) -> int | None:
    """申报公历年若在表头出现则优先；否则取最新财年列（多数 10-K 左起第一组为最近财年）。"""
    if not col_groups:
        return None
    if filing_year in col_groups:
        return filing_year
    return max(col_groups.keys())


def _first_numeric_in_columns(
    df: pd.DataFrame, row: int, cols: list[int]
) -> float | None:
    for c in sorted(cols):
        v = _parse_money_cell(df.iloc[row, c])
        if v is not None:
            return v
    return None


def _find_row_by_label_substrings(
    df: pd.DataFrame,
    patterns: list[str],
    *,
    exclude: tuple[str, ...] = (),
) -> int | None:
    """按行标签子串匹配首行；exclude 用于排除误匹配（如脚注行）。"""
    for r in range(len(df)):
        lab = _row_label(df, r)
        if any(e in lab for e in exclude):
            continue
        for p in patterns:
            if p in lab:
                return r
    return None


def _get_line_value(
    df: pd.DataFrame,
    col_groups: dict[int, list[int]],
    fiscal_year: int,
    patterns: list[str],
    *,
    exclude: tuple[str, ...] = (),
) -> float | None:
    row = _find_row_by_label_substrings(df, patterns, exclude=exclude)
    if row is None:
        return None
    cols = col_groups.get(fiscal_year)
    if not cols:
        return None
    return _first_numeric_in_columns(df, row, cols)


def _gross_margin_ratio(
    inc: pd.DataFrame,
    col_groups: dict[int, list[int]],
    fiscal_year: int,
    revenue: float | None,
) -> float | None:
    """优先用「毛利 / 营收」；若存在显式毛利率行且为小数则直接采用。"""
    row_gm = _find_row_by_label_substrings(
        inc,
        ["gross margin", "gross profit"],
        exclude=("percent", "%"),
    )
    if row_gm is not None:
        cols = col_groups.get(fiscal_year) or []
        raw = _first_numeric_in_columns(inc, row_gm, cols)
        if raw is not None and revenue not in (None, 0.0):
            if abs(raw) <= 1.0:
                return float(raw)
            return raw / revenue
    return None


def _ebitda_from_lines(
    inc: pd.DataFrame,
    cf: pd.DataFrame | None,
    cg_i: dict[int, list[int]],
    cg_c: dict[int, list[int]] | None,
    fiscal_year: int,
) -> float | None:
    """先找 EBITDA 行；否则 Operating income + CF 中折旧摊销（近似）。"""
    e = _get_line_value(
        inc,
        cg_i,
        fiscal_year,
        ["ebitda"],
        exclude=("margin", "ratio"),
    )
    if e is not None:
        return e

    op = _get_line_value(inc, cg_i, fiscal_year, ["operating income", "operating profit"])
    if op is None or cg_c is None:
        return None
    da = _get_line_value(
        cf,
        cg_c,
        fiscal_year,
        ["depreciation and amortization", "depreciation, amortization"],
    )
    if da is None:
        return None
    return op + da


def _capex_abs(
    cf: pd.DataFrame, cg_c: dict[int, list[int]], fiscal_year: int
) -> float | None:
    """资本性支出取绝对值（报表中常为负数）。"""
    raw = _get_line_value(
        cf,
        cg_c,
        fiscal_year,
        [
            "payments for acquisition of property",
            "purchases of property",
            "capital expenditure",
            "payments related to acquisition of property",
        ],
    )
    if raw is None:
        return None
    return abs(raw)


def _net_debt_to_equity(
    bs: pd.DataFrame, cg_b: dict[int, list[int]], fiscal_year: int
) -> float | None:
    """
    净负债/权益 ≈（有息负债合计 − 现金 − 有价证券）/ 股东权益合计。
    适配 Apple 式：Commercial paper + 多行 Term debt；现金 + 多行 Marketable securities。
    """
    cols = cg_b.get(fiscal_year)
    if not cols:
        return None

    debt_keys = (
        "commercial paper",
        "term debt",
        "long-term debt",
        "short-term borrow",
        "notes payable",
    )
    ms_key = "marketable securities"

    debt_sum = 0.0
    cash_sum = 0.0
    equity: float | None = None

    for r in range(len(bs)):
        lab = _row_label(bs, r)
        v = _first_numeric_in_columns(bs, r, cols)
        if v is None:
            continue
        # 须排除「负债与权益合计」行，否则会误把 352583 当成股东权益
        if (
            "total liabilities" not in lab
            and ("total shareholders" in lab or "total stockholders" in lab)
            and "equity" in lab
        ):
            equity = v
            continue
        if any(k in lab for k in debt_keys):
            debt_sum += v
            continue
        if lab.startswith("cash and cash equivalents"):
            cash_sum += v
            continue
        # ix 表格常见双列标签：「marketable securities marketable securities」
        if lab.startswith(ms_key):
            cash_sum += v
            continue

    if equity in (None, 0.0):
        return None
    net_debt = debt_sum - cash_sum
    return net_debt / equity


def get_financial_statements(ticker: str, year: int) -> AnnualFinancials | None:
    """
    从与 ``year``（10-K 申报公历年，与 ``get_10k_text`` 一致）对应的主文档 HTML 中，
    解析 Item 8 常见合并报表，填充 ``AnnualFinancials``。

    - 使用 ``pandas.read_html`` 解析表格；失败或缺字段时返回 None 或对应字段为 None。
    - 依赖 ``data/raw/10k/{TICKER}_{year}_full.html`` 缓存避免重复下载。
    """
    sym = (ticker or "").strip().upper()
    if not sym:
        return None

    try:
        html = _get_10k_raw_html(sym, year)
    except SecEdgarError:
        return None
    except OSError:
        return None

    # 部分 10-K 带 XML 声明，StringIO 传入 lxml 会报错，需先剥离
    html_for_tables = re.sub(
        r"^\s*<\?xml[^>]*\?>\s*",
        "",
        html,
        count=1,
        flags=re.IGNORECASE | re.DOTALL,
    )
    try:
        with warnings.catch_warnings():
            warnings.simplefilter("ignore", XMLParsedAsHTMLWarning)
            tables = pd.read_html(StringIO(html_for_tables), flavor="lxml")
    except (ValueError, ImportError, OSError):
        return None

    income, cashflow, balance = _pick_statement_tables(tables)
    if income is None:
        return None

    cg_i = _column_groups_by_fiscal_year(income)
    fiscal_year = _resolve_fiscal_year_for_columns(cg_i, year)
    if fiscal_year is None:
        return None

    revenue = _get_line_value(
        income,
        cg_i,
        fiscal_year,
        ["total net sales", "total revenue", "net revenue"],
        exclude=("cost",),
    )
    gross_margin = _gross_margin_ratio(income, cg_i, fiscal_year, revenue)

    cg_c = _column_groups_by_fiscal_year(cashflow) if cashflow is not None else None
    cg_b = _column_groups_by_fiscal_year(balance) if balance is not None else None

    ebitda = _ebitda_from_lines(income, cashflow, cg_i, cg_c, fiscal_year)

    capex: float | None = None
    if cashflow is not None and cg_c:
        capex = _capex_abs(cashflow, cg_c, fiscal_year)

    net_de: float | None = None
    if balance is not None and cg_b:
        net_de = _net_debt_to_equity(balance, cg_b, fiscal_year)

    return AnnualFinancials(
        year=fiscal_year,
        revenue=revenue,
        ebitda=ebitda,
        gross_margin=gross_margin,
        capex=capex,
        net_debt_to_equity=net_de,
    )
