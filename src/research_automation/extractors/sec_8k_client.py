"""
从 SEC 8-K 提取电话会/业绩说明正文：

- **EDGAR 直连**：``data.sec.gov/submissions`` + Archives（无需 sec-api.io）。
- **sec-api.io**：``/full-text-search`` 检索 8-K 附件元数据，再下载 ``filingUrl``（需 ``SEC_API_KEY``）。

EDGAR 下载须 ``SEC_EDGAR_USER_AGENT``；sec-api 免费层约 100 次/日，请依赖缓存与 ``_throttle_sec_api``。
"""
from __future__ import annotations

import json
import logging
import os
import re
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any
from urllib.parse import quote

import requests
from bs4 import BeautifulSoup
from dotenv import load_dotenv

from research_automation.extractors import sec_edgar
from research_automation.extractors.sec_edgar import SecEdgarError

logger = logging.getLogger(__name__)

load_dotenv(Path(__file__).resolve().parents[3] / ".env", override=False)

SEC_API_URL = "https://api.sec-api.io"
_SEC_API_MIN_INTERVAL_SEC = 1.2
_last_sec_api_mono: float = 0.0

SEC_ARCHIVES_DATA = "https://www.sec.gov/Archives/edgar/data"
# sec_edgar.py -> extractors -> research_automation -> src -> 项目根
_PROJECT_ROOT = Path(__file__).resolve().parents[3]
_CACHE_DIR = _PROJECT_ROOT / "data" / "raw" / "8k_transcripts"

_Q_HINTS = (
    "conference call",
    "earnings call",
    "transcript",
    "operator",
    "forward-looking",
    "safe harbor",
    "question-and-answer",
    "question and answer",
)


def _today_utc() -> datetime.date:
    return datetime.now(timezone.utc).date()


def _filing_age_days(filing_date: str) -> int:
    fd = datetime.strptime(filing_date[:10], "%Y-%m-%d").date()
    return (_today_utc() - fd).days


def _iter_recent_8k(
    submissions: dict[str, Any],
) -> list[tuple[str, str, str]]:
    """自新到旧列出 (accessionNumber, filingDate YYYY-MM-DD, primaryDocument)。"""
    recent = submissions.get("filings", {}).get("recent")
    if not isinstance(recent, dict):
        return []
    forms = recent.get("form") or []
    dates = recent.get("filingDate") or []
    accs = recent.get("accessionNumber") or []
    docs = recent.get("primaryDocument") or []
    n = min(len(forms), len(dates), len(accs), len(docs))
    out: list[tuple[str, str, str]] = []
    for i in range(n):
        if str(forms[i]).upper() not in ("8-K", "8-K/A"):
            continue
        fd = str(dates[i])[:10]
        out.append((str(accs[i]), fd, str(docs[i])))
    return out


def _cache_file(ticker: str, filing_date: str) -> Path:
    sym = (ticker or "").strip().upper()
    _CACHE_DIR.mkdir(parents=True, exist_ok=True)
    return _CACHE_DIR / f"{sym}_{filing_date}.txt"


def _read_cache(path: Path) -> str | None:
    if path.is_file() and path.stat().st_size > 0:
        return path.read_text(encoding="utf-8", errors="replace")
    return None


def _write_cache(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")


def _exhibit_score(filename: str) -> int:
    fn = (filename or "").lower()
    s = 0
    if "99.1" in fn or "991" in fn or "ex-99.1" in fn or "ex991" in fn:
        s += 100
    if "transcript" in fn:
        s += 95
    if "ex-99" in fn or "ex99" in fn:
        s += 70
    if "earnings" in fn and ("release" in fn or "call" in fn):
        s += 55
    if "exhibit" in fn and "99" in fn:
        s += 45
    if fn.endswith((".htm", ".html")):
        s += 5
    return s


def _index_items_from_json(raw: bytes) -> list[dict[str, Any]]:
    try:
        data = json.loads(raw.decode("utf-8"))
    except (json.JSONDecodeError, UnicodeDecodeError):
        return []
    items: list[dict[str, Any]] = []
    d = data.get("directory")
    if isinstance(d, dict):
        item = d.get("item")
        if isinstance(item, list):
            items.extend(x for x in item if isinstance(x, dict))
        elif isinstance(item, dict):
            items.append(item)
    return items


def _href_names_from_index_html(raw: bytes) -> list[str]:
    try:
        html = raw.decode("utf-8", errors="replace")
    except Exception:
        return []
    soup = BeautifulSoup(html, "lxml")
    names: list[str] = []
    for a in soup.find_all("a", href=True):
        h = str(a.get("href") or "").strip()
        if not h or h.startswith("http") or h in ("/", "../"):
            continue
        base = h.split("/")[-1].split("?")[0]
        if base and base not in ("index.json", "index.htm", "index.html"):
            names.append(base)
    return names


def _list_candidate_filenames(
    cik: int, accession: str, primary_document: str
) -> list[str]:
    acc_dir = accession.replace("-", "")
    base = f"{SEC_ARCHIVES_DATA}/{cik}/{acc_dir}/"
    names: list[str] = []
    for idx_name in ("index.json", "index.htm"):
        try:
            raw = sec_edgar._throttled_sec_get(base + idx_name)
        except SecEdgarError:
            continue
        if idx_name.endswith(".json"):
            for it in _index_items_from_json(raw):
                nm = it.get("name")
                if isinstance(nm, str) and nm.strip():
                    names.append(nm.strip())
        else:
            names.extend(_href_names_from_index_html(raw))
    # 去重保序
    seen: set[str] = set()
    uniq: list[str] = []
    for nm in names:
        if nm == primary_document:
            continue
        low = nm.lower()
        if low in ("index.json", "index.htm", "index.html"):
            continue
        if nm not in seen:
            seen.add(nm)
            uniq.append(nm)
    uniq.sort(key=_exhibit_score, reverse=True)
    return uniq


def _html_to_text(raw: bytes) -> str:
    try:
        html = raw.decode("utf-8")
    except UnicodeDecodeError:
        html = raw.decode("latin-1", errors="replace")
    soup = BeautifulSoup(html, "lxml")
    for tag in soup(["script", "style"]):
        tag.decompose()
    return soup.get_text("\n", strip=True)


def _html_to_text_keep_paragraphs(raw: bytes) -> str:
    """HTML → 纯文本，块级元素间保留空行。"""
    try:
        html = raw.decode("utf-8")
    except UnicodeDecodeError:
        html = raw.decode("latin-1", errors="replace")
    soup = BeautifulSoup(html, "lxml")
    for tag in soup(["script", "style", "head"]):
        tag.decompose()
    for br in soup.find_all("br"):
        br.replace_with("\n")
    chunks: list[str] = []
    for el in soup.find_all(["p", "div", "li", "h1", "h2", "h3", "h4", "tr"]):
        tx = el.get_text(" ", strip=True)
        if tx:
            chunks.append(tx)
    if len(chunks) >= 2:
        body = "\n\n".join(chunks)
    else:
        body = soup.get_text("\n", strip=True)
    body = re.sub(r"\n{3,}", "\n\n", body)
    return body.strip()


def _fetch_exhibit_bytes(cik: int, accession: str, filename: str) -> bytes | None:
    acc_dir = accession.replace("-", "")
    url = f"{SEC_ARCHIVES_DATA}/{cik}/{acc_dir}/{quote(filename)}"
    try:
        return sec_edgar._throttled_sec_get(url)
    except SecEdgarError as e:
        logger.debug("8-K 附件下载失败 %s: %s", url, e)
        return None


def _looks_like_transcript_body(text: str) -> bool:
    t = (text or "").strip()
    if len(t) < 500:
        return False
    tl = t.lower()
    if sum(h in tl for h in _Q_HINTS) >= 1:
        return True
    if len(t) >= 2000 and any(
        k in tl for k in ("revenue", "quarter", "fiscal", "eps", "guidance")
    ):
        return True
    return False


def _text_covers_fiscal_quarter(text: str, year: int, quarter: int) -> bool:
    if str(year) not in text:
        return False
    tl = text.lower()
    qmap = {
        1: ("first quarter", "q1", "1st quarter"),
        2: ("second quarter", "q2", "2nd quarter"),
        3: ("third quarter", "q3", "3rd quarter"),
        4: ("fourth quarter", "q4", "4th quarter"),
    }
    for ph in qmap.get(quarter, ()):
        if ph in tl:
            return True
    if re.search(
        rf"\bq\s*{quarter}\b[^\n]{{0,80}}{year}|{year}[^\n]{{0,80}}\bq\s*{quarter}\b",
        tl,
        re.I,
    ):
        return True
    return False


def _best_text_from_filing(
    cik: int,
    accession: str,
    primary_document: str,
) -> str | None:
    for fn in _list_candidate_filenames(cik, accession, primary_document):
        low = fn.lower()
        if low.endswith((".xml", ".xsd", ".jpg", ".png", ".gif", ".pdf")):
            continue
        raw = _fetch_exhibit_bytes(cik, accession, fn)
        if not raw or len(raw) < 200:
            continue
        if low.endswith((".htm", ".html")) or b"<html" in raw[:2000].lower():
            txt = _html_to_text(raw)
        else:
            try:
                txt = raw.decode("utf-8", errors="replace")
            except Exception:
                txt = raw.decode("latin-1", errors="replace")
        txt = txt.strip()
        if _looks_like_transcript_body(txt):
            return txt
    return None


def search_8k_transcript(
    ticker: str,
    lookback_days: int = 7,
    *,
    fiscal_year: int | None = None,
    fiscal_quarter: int | None = None,
    max_8k_to_scan: int = 80,
) -> str | None:
    """
    在 SEC 8-K 申报中查找 EX-99.x 等附件，提取电话会/业绩说明类正文（纯文本）。

    - 无 ``fiscal_year``/``fiscal_quarter``：只考虑最近 ``lookback_days`` 天内提交的 8-K。
    - 指定财季时：在约 ``max(lookback_days, 500)`` 天内的 8-K 中扫描，且正文须似含该财年/季表述。

    成功时写入缓存 ``data/raw/8k_transcripts/{TICKER}_{filing_date}.txt``。
    """
    sym = (ticker or "").strip().upper()
    if not sym:
        return None

    try:
        cik = sec_edgar.get_cik(sym)
    except SecEdgarError as e:
        logger.warning("8-K 逐字稿：无法解析 CIK ticker=%s: %s", sym, e)
        return None

    try:
        submissions = sec_edgar._fetch_submissions(cik)
    except SecEdgarError as e:
        logger.warning("8-K 逐字稿：submissions 失败 ticker=%s: %s", sym, e)
        return None

    rows = _iter_recent_8k(submissions)
    if not rows:
        return None
    # 始终自新到旧尝试（submissions 数组顺序因数据源而异）
    rows.sort(key=lambda r: r[1], reverse=True)

    if fiscal_year is not None and fiscal_quarter is not None:
        max_age = max(int(lookback_days), 500)
    else:
        max_age = max(int(lookback_days), 1)

    tried = 0
    for accession, filing_date, primary_doc in rows:
        if _filing_age_days(filing_date) > max_age:
            continue
        tried += 1
        if tried > max_8k_to_scan:
            break

        cpath = _cache_file(sym, filing_date)
        cached = _read_cache(cpath)
        if cached and _looks_like_transcript_body(cached):
            if fiscal_year is None or fiscal_quarter is None:
                return cached.strip()
            if _text_covers_fiscal_quarter(cached, fiscal_year, fiscal_quarter):
                return cached.strip()

        text = _best_text_from_filing(cik, accession, primary_doc)
        if not text:
            continue
        if fiscal_year is not None and fiscal_quarter is not None:
            if not _text_covers_fiscal_quarter(text, fiscal_year, fiscal_quarter):
                continue
        _write_cache(cpath, text)
        logger.info(
            "8-K 逐字稿命中 ticker=%s filing_date=%s accession=%s",
            sym,
            filing_date,
            accession,
        )
        return text.strip()

    return None


# ---------------------------------------------------------------------------
# sec-api.io Full-Text Search（需 SEC_API_KEY）
# ---------------------------------------------------------------------------


def _sec_api_key() -> str | None:
    k = (os.getenv("SEC_API_KEY") or "").strip()
    return k or None


def _throttle_before_sec_api_call() -> None:
    global _last_sec_api_mono

    now = time.monotonic()
    wait = _SEC_API_MIN_INTERVAL_SEC - (now - _last_sec_api_mono)
    if wait > 0:
        time.sleep(wait)
    _last_sec_api_mono = time.monotonic()


def _sec_api_exhibit_row(f: dict[str, Any]) -> bool:
    desc = (f.get("description") or "").lower()
    typ = (f.get("type") or "").lower()
    if "transcript" in desc:
        return True
    if "ex-99" in desc or "exhibit 99" in desc or "99.1" in desc:
        return True
    if "ex-99" in typ or "99.1" in typ:
        return True
    if typ.startswith("ex-99"):
        return True
    return False


def _sec_api_row_priority(f: dict[str, Any]) -> tuple[int, str]:
    """越大越优先；第二键 filedAt 降序。"""
    desc = (f.get("description") or "").lower()
    typ = (f.get("type") or "").lower()
    score = 0
    if "99.1" in desc or "99.1" in typ:
        score += 100
    if "transcript" in desc:
        score += 95
    if "ex-99" in desc or "ex-99" in typ:
        score += 70
    if "earnings" in desc or "call" in desc:
        score += 40
    fd = str(f.get("filedAt") or "")[:10]
    return (score, fd)


def _download_filing_url(url: str) -> bytes | None:
    if not url or "sec.gov" not in url.lower():
        return None
    try:
        r = requests.get(
            url,
            headers={
                "User-Agent": sec_edgar._user_agent(),
                "Accept": "text/html,application/xhtml+xml,text/plain;q=0.9,*/*;q=0.8",
                "Referer": "https://www.sec.gov/",
            },
            timeout=90,
        )
        r.raise_for_status()
        return r.content
    except requests.RequestException as e:
        logger.warning("下载 SEC 附件失败 url=%s: %s", url[:80], e)
        return None


def _bytes_to_text_sec_api(raw: bytes) -> str:
    low = raw[:4000].lower()
    if b"<html" in low or b"<!doctype html" in low or b"<body" in low:
        return _html_to_text_keep_paragraphs(raw)
    try:
        return raw.decode("utf-8", errors="replace").strip()
    except Exception:
        return raw.decode("latin-1", errors="replace").strip()


def fetch_transcript_from_8k(
    ticker: str,
    lookback_days: int = 14,
    *,
    fiscal_year: int | None = None,
    fiscal_quarter: int | None = None,
) -> str | None:
    """
    使用 sec-api.io ``POST /full-text-search`` 检索近期 8-K 附件（EX-99 / transcript 等），
    下载 ``filingUrl`` 并抽取纯文本。无 ``SEC_API_KEY`` 或失败时返回 ``None``。

    命中后缓存至 ``data/raw/8k_transcripts/{TICKER}_{filedAt}.txt``。
    若提供 ``fiscal_year`` / ``fiscal_quarter``，将扩大检索窗口并校验正文是否似对应该季。
    """
    sym = (ticker or "").strip().upper()
    if not sym:
        return None
    key = _sec_api_key()
    if not key:
        return None

    try:
        cik_int = sec_edgar.get_cik(sym)
    except SecEdgarError as e:
        logger.warning("sec-api 8-K：CIK 失败 ticker=%s: %s", sym, e)
        return None

    end_d = _today_utc()
    if fiscal_year is not None and fiscal_quarter is not None:
        span = max(int(lookback_days), 500)
    else:
        span = max(int(lookback_days), 1)
    start_d = end_d - timedelta(days=span)

    _throttle_before_sec_api_call()
    payload: dict[str, Any] = {
        "query": "earnings OR conference OR transcript OR revenue OR quarter OR results",
        "formTypes": ["8-K", "8-K/A"],
        "startDate": start_d.isoformat(),
        "endDate": end_d.isoformat(),
        "ciks": [f"{cik_int:010d}"],
        "page": "1",
    }
    try:
        r = requests.post(
            f"{SEC_API_URL}/full-text-search",
            headers={
                "Authorization": key,
                "Content-Type": "application/json",
                "User-Agent": sec_edgar._user_agent(),
            },
            json=payload,
            timeout=60,
        )
        r.raise_for_status()
        data = r.json()
    except (requests.RequestException, ValueError) as e:
        logger.warning("sec-api full-text-search 失败 ticker=%s: %s", sym, e)
        return None

    filings = data.get("filings")
    if not isinstance(filings, list):
        return None

    candidates: list[dict[str, Any]] = []
    for f in filings:
        if not isinstance(f, dict):
            continue
        if str(f.get("formType") or "").upper() not in ("8-K", "8-K/A"):
            continue
        ft = (f.get("ticker") or "").strip().upper()
        if ft and ft != sym:
            continue
        if not _sec_api_exhibit_row(f):
            continue
        candidates.append(f)

    if not candidates:
        return None

    candidates.sort(
        key=lambda x: (_sec_api_row_priority(x)[0], _sec_api_row_priority(x)[1]),
        reverse=True,
    )

    for f in candidates:
        filed_at = str(f.get("filedAt") or "")[:10]
        if not filed_at:
            continue
        if _filing_age_days(filed_at) > span:
            continue
        url = str(f.get("filingUrl") or "").strip()
        if not url:
            continue

        cpath = _cache_file(sym, filed_at)
        cached = _read_cache(cpath)
        if cached and _looks_like_transcript_body(cached):
            if fiscal_year is None or fiscal_quarter is None:
                return cached.strip()
            if _text_covers_fiscal_quarter(cached, fiscal_year, fiscal_quarter):
                return cached.strip()

        raw = _download_filing_url(url)
        if not raw or len(raw) < 200:
            continue
        text = _bytes_to_text_sec_api(raw)
        if not _looks_like_transcript_body(text):
            continue
        if fiscal_year is not None and fiscal_quarter is not None:
            if not _text_covers_fiscal_quarter(text, fiscal_year, fiscal_quarter):
                continue
        _write_cache(cpath, text)
        logger.info(
            "sec-api 8-K 逐字稿命中 ticker=%s filedAt=%s type=%s",
            sym,
            filed_at,
            f.get("type"),
        )
        return text.strip()

    return None
