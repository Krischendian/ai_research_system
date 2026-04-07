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
from pathlib import Path
from typing import Any

from bs4 import BeautifulSoup, XMLParsedAsHTMLWarning

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


def _extract_item1_business(html: str) -> str:
    """
    从 10-K 主文档 HTML 中截取 Item 1 Business 至 Item 1A Risk Factors（不含）之间的文本。
    目录中常出现简短「Item 1. Business」链接，故优先取**末尾**匹配段且长度足够者。
    """
    with warnings.catch_warnings():
        warnings.simplefilter("ignore", XMLParsedAsHTMLWarning)
        soup = BeautifulSoup(html, "lxml")
    text = soup.get_text("\n")
    text = re.sub(r"[\xa0\t\f\r]+", " ", text)
    text = re.sub(r"\n{3,}", "\n\n", text)

    pat_start = re.compile(r"(?is)\bitem\s*1\.?\s*business\b")
    pat_end = re.compile(r"(?is)\bitem\s*1a\.?\s*risk\s*factors\b")
    candidates = list(pat_start.finditer(text))
    if not candidates:
        return ""

    def slice_item1(m_start: re.Match[str]) -> str:
        body = text[m_start.start() :]
        m_e = pat_end.search(body, 80)
        if m_e:
            return body[: m_e.start()].strip()
        tail = body[200:]
        m_e2 = re.search(r"(?is)\bitem\s*2\.?\s*properties\b", tail)
        if m_e2:
            return body[: 200 + m_e2.start()].strip()
        return body.strip()

    best = ""
    for m in reversed(candidates):
        chunk = slice_item1(m)
        if len(chunk) > len(best):
            best = chunk
        if len(chunk) > 800:
            break
    return best if best else slice_item1(candidates[0])


def _cache_path(ticker: str, year: int) -> Path:
    sym = (ticker or "").strip().upper()
    root = _project_root() / "data" / "raw" / "10k"
    root.mkdir(parents=True, exist_ok=True)
    return root / f"{sym}_{year}.txt"


def get_10k_text(ticker: str, year: int) -> str:
    """
    拉取指定公历年附近提交的 10-K，解析「Item 1. Business」为纯文本。

    - 使用 CIK 映射（预置 + SEC ``company_tickers.json`` 缓存）。
    - 成功后写入 ``data/raw/10k/{TICKER}_{year}.txt``（按请求年份命名），命中则直接读缓存。
    """
    sym = (ticker or "").strip().upper()
    if not sym:
        raise SecEdgarError("ticker 不能为空")

    cache = _cache_path(sym, year)
    if cache.exists() and cache.stat().st_size > 0:
        return cache.read_text(encoding="utf-8", errors="replace")

    cik = get_cik(sym)
    subs = _fetch_submissions(cik)
    accession, primary_doc = _resolve_10k_filing(subs, year)
    if not primary_doc:
        raise SecEdgarError("SEC submissions 中缺少 primaryDocument。")

    raw_doc = _fetch_primary_document(cik, accession, primary_doc)
    item1 = _extract_item1_business(raw_doc.replace("\r\n", "\n"))

    if not item1 or len(item1.strip()) < 100:
        raise SecEdgarError(
            "未能从 10-K 主文档中解析出足够长度的 Item 1 Business（请检查表格版式或招股书类型）。"
        )

    cache.write_text(item1, encoding="utf-8")
    return item1
