"""
Financial Modeling Prep (FMP) **stable** 端点：财务报表、财报电话会逐字稿等。

新密钥对旧版 ``/api/v3/...`` 常返回 403，故统一使用 ``/stable/...``。
需环境变量 ``FMP_API_KEY``；财务与逐字稿在无密钥或失败时分别返回 ``[]`` / ``None``。
"""
from __future__ import annotations

import logging
import os
import re
from pathlib import Path
from typing import Any

import requests
from dotenv import load_dotenv

from research_automation.models.financial import AnnualFinancials

logger = logging.getLogger(__name__)

load_dotenv(Path(__file__).resolve().parents[3] / ".env", override=False)

BASE_URL = "https://financialmodelingprep.com/stable"
_DEFAULT_TIMEOUT_SEC = 30.0
_REQUEST_HEADERS = {
    "Accept": "application/json",
    "User-Agent": "research-automation/1.0 (+https://financialmodelingprep.com/developer/docs/)",
}


def _api_key() -> str | None:
    k = (os.getenv("FMP_API_KEY") or "").strip()
    return k or None


def _num(v: Any) -> float | None:
    if v is None:
        return None
    if isinstance(v, bool):
        return None
    try:
        x = float(v)
    except (TypeError, ValueError):
        return None
    if x != x:  # NaN
        return None
    return x


def _year_from_row(row: dict[str, Any]) -> int | None:
    fy = row.get("fiscalYear")
    if fy is not None and str(fy).strip():
        try:
            return int(str(fy).strip()[:4])
        except ValueError:
            pass
    cy = row.get("calendarYear")
    if cy is not None and str(cy).strip():
        try:
            return int(str(cy).strip()[:4])
        except ValueError:
            pass
    d = row.get("date")
    if isinstance(d, str) and len(d) >= 4:
        try:
            return int(d[:4])
        except ValueError:
            pass
    return None


def _normalize_margin_ratio(raw: float | None) -> float | None:
    if raw is None:
        return None
    if raw > 1.5:
        return raw / 100.0
    return raw


def _fetch_statement(endpoint: str, ticker: str, limit: int) -> list[dict[str, Any]]:
    key = _api_key()
    if not key:
        return []
    sym = (ticker or "").strip().upper()
    params: dict[str, str | int] = {
        "symbol": sym,
        "apikey": key,
        "limit": int(limit),
        "period": "annual",
    }
    url = f"{BASE_URL}/{endpoint}"
    try:
        r = requests.get(
            url,
            params=params,
            headers=_REQUEST_HEADERS,
            timeout=_DEFAULT_TIMEOUT_SEC,
        )
        r.raise_for_status()
        data = r.json()
    except (requests.RequestException, ValueError) as e:
        logger.warning(
            "FMP 请求失败 endpoint=%s ticker=%s: %s",
            endpoint,
            sym,
            e,
        )
        return []

    if isinstance(data, dict):
        if data.get("Error Message"):
            logger.warning(
                "FMP 返回错误 ticker=%s endpoint=%s: %s",
                sym,
                endpoint,
                data.get("Error Message"),
            )
            return []
        # 部分端点偶发返回「单条年度对象」而非数组；有财年字段则按一行处理
        if _year_from_row(data) is not None:
            return [data]
        logger.warning(
            "FMP 返回未识别的 dict（无 Error Message、无可用财年）endpoint=%s ticker=%s keys=%s",
            endpoint,
            sym,
            list(data.keys())[:24],
        )
        return []

    if not isinstance(data, list):
        return []

    return [x for x in data if isinstance(x, dict)]


def _index_by_year(rows: list[dict[str, Any]]) -> dict[int, dict[str, Any]]:
    out: dict[int, dict[str, Any]] = {}
    for row in rows:
        y = _year_from_row(row)
        if y is not None:
            out[y] = row
    return out


def _ebitda(inc: dict[str, Any], cf: dict[str, Any] | None) -> float | None:
    e = _num(inc.get("ebitda"))
    if e is not None:
        return e
    op = _num(inc.get("operatingIncome"))
    if op is None or cf is None:
        return None
    da = _num(cf.get("depreciationAndAmortization"))
    if da is None:
        da = _num(cf.get("depreciationDepletionAndAmortization"))
    if da is None:
        return None
    return op + da


def _gross_margin(
    inc: dict[str, Any], metrics: dict[str, Any] | None
) -> float | None:
    rev = _num(inc.get("revenue"))
    gp = _num(inc.get("grossProfit"))
    if rev is not None and rev != 0 and gp is not None:
        return gp / rev
    if metrics:
        m = _num(
            metrics.get("grossProfitMargin")
            or metrics.get("grossProfitMarginRatio")
        )
        return _normalize_margin_ratio(m)
    return None


def _capex(cf: dict[str, Any] | None) -> float | None:
    if not cf:
        return None
    raw = _num(cf.get("capitalExpenditure"))
    if raw is None:
        return None
    return abs(raw)


def _net_debt_to_equity(
    bal: dict[str, Any] | None, metrics: dict[str, Any] | None
) -> float | None:
    if metrics:
        nd = _num(metrics.get("netDebtToEquity"))
        if nd is not None:
            return nd
    if not bal:
        return None
    equity = _num(bal.get("totalStockholdersEquity"))
    if equity is None or equity == 0:
        return None
    debt = _num(bal.get("totalDebt"))
    if debt is None:
        return None
    cash_st = _num(bal.get("cashAndShortTermInvestments"))
    if cash_st is None:
        c1 = _num(bal.get("cashAndCashEquivalents")) or 0.0
        c2 = _num(bal.get("shortTermInvestments")) or 0.0
        cash_st = c1 + c2
    return (debt - cash_st) / equity


def get_financials(ticker: str, years: int = 3) -> list[AnnualFinancials]:
    """
    获取公司年度财务指标（收入、EBITDA、毛利率、资本支出、净负债/权益）。

    调用 FMP **stable** 端点（与 ``/stable`` 文档一致，查询参数 ``symbol``）：

    - ``income-statement``（利润表）
    - ``cash-flow-statement``（现金流）
    - ``balance-sheet-statement``（资产负债表）
    - ``key-metrics``（毛利率、净负债/权益等）

    按 ``fiscalYear`` / ``calendarYear`` / ``date`` 对齐财年（``AnnualFinancials.year``），
    降序返回至多 ``years`` 条；字段缺失为 ``None``。
    """
    sym = (ticker or "").strip().upper()
    if not sym or years < 1:
        return []

    if not _api_key():
        return []

    lim = max(years, 1) + 2

    income_rows = _fetch_statement("income-statement", sym, lim)
    if not income_rows:
        return []

    cash_rows = _fetch_statement("cash-flow-statement", sym, lim)
    bal_rows = _fetch_statement("balance-sheet-statement", sym, lim)
    metrics_rows = _fetch_statement("key-metrics", sym, lim)

    by_i = _index_by_year(income_rows)
    by_c = _index_by_year(cash_rows)
    by_b = _index_by_year(bal_rows)
    by_m = _index_by_year(metrics_rows)

    fiscal_years = sorted(by_i.keys(), reverse=True)[:years]

    result: list[AnnualFinancials] = []
    for y in fiscal_years:
        inc = by_i[y]
        cf = by_c.get(y)
        bal = by_b.get(y)
        met = by_m.get(y)

        revenue = _num(inc.get("revenue"))
        ebitda = _ebitda(inc, cf)
        gross_margin = _gross_margin(inc, met)
        capex = _capex(cf)
        nd_eq = _net_debt_to_equity(bal, met)

        result.append(
            AnnualFinancials(
                year=y,
                revenue=revenue,
                ebitda=ebitda,
                capex=capex,
                gross_margin=gross_margin,
                net_debt_to_equity=nd_eq,
            )
        )

    return result


def get_segment_revenue(ticker: str, year: int) -> list[dict[str, Any]] | None:
    """
    获取公司指定财年的产品/业务线营收拆分（FMP stable ``revenue-product-segmentation``）。

    返回 ``[{"segment": "iPhone", "percentage": 52.3, "absolute": 2e10}, ...]``（``percentage`` 为占当期
    披露分部营收合计的百分比）；无密钥、HTTP 非 200、无对应财年或分部数据时返回 ``None``。
    """
    key = _api_key()
    if not key:
        return None
    sym = (ticker or "").strip().upper()
    if not sym:
        return None
    try:
        y = int(year)
    except (TypeError, ValueError):
        return None

    url = f"{BASE_URL}/revenue-product-segmentation"
    params: dict[str, str] = {"symbol": sym, "apikey": key}
    try:
        r = requests.get(
            url,
            params=params,
            headers=_REQUEST_HEADERS,
            timeout=_DEFAULT_TIMEOUT_SEC,
        )
        if r.status_code != 200:
            logger.debug(
                "FMP revenue-product-segmentation 非 200 ticker=%s status=%s",
                sym,
                r.status_code,
            )
            return None
        data = r.json()
    except (requests.RequestException, ValueError) as e:
        logger.warning(
            "FMP revenue-product-segmentation 请求失败 ticker=%s: %s",
            sym,
            e,
        )
        return None

    if isinstance(data, dict):
        if data.get("Error Message"):
            logger.debug(
                "FMP revenue-product-segmentation 错误 ticker=%s: %s",
                sym,
                data.get("Error Message"),
            )
            return None
        # 有时返回单财年对象 ``{fiscalYear, data: {...}}`` 而非数组
        if isinstance(data.get("data"), dict) and data.get("fiscalYear") is not None:
            data = [data]
        else:
            logger.debug(
                "FMP revenue-product-segmentation 未识别的 dict ticker=%s keys=%s",
                sym,
                list(data.keys())[:24],
            )
            return None

    if not isinstance(data, list):
        return None

    target: dict[str, Any] | None = None
    for row in data:
        if not isinstance(row, dict):
            continue
        fy = row.get("fiscalYear")
        try:
            if int(fy) != y:
                continue
        except (TypeError, ValueError):
            continue
        target = row
        break

    if not target:
        return None

    raw_data = target.get("data")
    if not isinstance(raw_data, dict) or not raw_data:
        return None

    absolutes: list[tuple[str, float]] = []
    for seg_name, val in raw_data.items():
        name = str(seg_name).strip()
        if not name:
            continue
        amt = _num(val)
        if amt is None or amt <= 0:
            continue
        absolutes.append((name, float(amt)))

    if len(absolutes) < 1:
        return None

    total = sum(a for _, a in absolutes)
    if total <= 0:
        return None

    out: list[dict[str, Any]] = []
    for name, amt in sorted(absolutes, key=lambda x: -x[1]):
        pct = round(amt / total * 100.0, 2)
        out.append(
            {
                "segment": name,
                "percentage": pct,
                "absolute": amt,
            }
        )
    return out


def get_geographic_revenue(ticker: str, year: int) -> list[dict[str, Any]] | None:
    """
    获取公司指定财年的地理收入拆分（FMP stable revenue-geographic-segmentation）。
    返回 [{"region": "North America", "percentage": 52.3, "absolute": 2e10}, ...]
    无数据时返回 None。
    """
    key = _api_key()
    if not key:
        return None
    sym = (ticker or "").strip().upper()
    if not sym:
        return None
    try:
        y = int(year)
    except (TypeError, ValueError):
        return None

    url = f"{BASE_URL}/revenue-geographic-segmentation"
    params: dict[str, str] = {"symbol": sym, "apikey": key}
    try:
        r = requests.get(
            url,
            params=params,
            headers=_REQUEST_HEADERS,
            timeout=_DEFAULT_TIMEOUT_SEC,
        )
        if r.status_code != 200:
            return None
        data = r.json()
    except (requests.RequestException, ValueError):
        return None

    if isinstance(data, dict):
        if data.get("Error Message"):
            return None
        if isinstance(data.get("data"), dict) and data.get("fiscalYear") is not None:
            data = [data]
        else:
            return None

    if not isinstance(data, list):
        return None

    target = None
    for row in data:
        if not isinstance(row, dict):
            continue
        try:
            if int(row.get("fiscalYear", 0)) == y:
                target = row
                break
        except (TypeError, ValueError):
            continue

    if not target:
        # 找最近一年
        candidates = []
        for row in data:
            if isinstance(row, dict):
                try:
                    candidates.append((int(row.get("fiscalYear", 0)), row))
                except (TypeError, ValueError):
                    pass
        if candidates:
            target = sorted(candidates, reverse=True)[0][1]
        else:
            return None

    raw_data = target.get("data")
    if not isinstance(raw_data, dict) or not raw_data:
        return None

    absolutes = []
    for region_name, val in raw_data.items():
        name = str(region_name).strip()
        if not name:
            continue
        amt = _num(val)
        if amt is None or amt <= 0:
            continue
        absolutes.append((name, float(amt)))

    if not absolutes:
        return None

    total = sum(a for _, a in absolutes)
    if total <= 0:
        return None

    return [
        {"region": name, "percentage": round(amt / total * 100.0, 2), "absolute": amt}
        for name, amt in sorted(absolutes, key=lambda x: -x[1])
    ]


# ---------------------------------------------------------------------------
# Earnings call transcript (stable)
# ---------------------------------------------------------------------------

_LABEL_PAREN_ROLE = re.compile(r"^(.+?)\s*[\(（]([^)）]{1,60})[\)）]\s*$")


def _split_label_to_speaker_position(label: str) -> tuple[str, str]:
    label = (label or "").strip()
    if not label:
        return "", ""
    m = _LABEL_PAREN_ROLE.match(label)
    if m:
        return m.group(1).strip(), m.group(2).strip()
    if " - " in label:
        a, b = label.split(" - ", 1)
        if len(a) <= 60 and len(b) <= 60:
            return a.strip(), b.strip()
    return label, ""


_COLON_SPEAKER_BLOCKS = re.compile(
    r"(?m)^(?P<label>[^\n:]{2,120}?):\s*",
)


def _split_plaintext_to_dialogues(text: str) -> list[dict[str, str]]:
    """将整段逐字稿按「行首 姓名:」切分为对话块（FMP 常见为单字符串 content）。"""
    text = (text or "").replace("\r\n", "\n").strip()
    if not text:
        return []

    matches = list(_COLON_SPEAKER_BLOCKS.finditer(text))
    if len(matches) >= 2:
        out: list[dict[str, str]] = []
        for i, m in enumerate(matches):
            raw_label = m.group("label").strip()
            sp, pos = _split_label_to_speaker_position(raw_label)
            start = m.end()
            end = matches[i + 1].start() if i + 1 < len(matches) else len(text)
            chunk = text[start:end].strip()
            if chunk:
                out.append({"speaker": sp, "position": pos, "text": chunk})
        if out:
            return out

    parts = re.split(r"\n\s*\n+", text)
    dialogues: list[dict[str, str]] = []
    for block in parts:
        block = block.strip()
        if not block:
            continue
        first, sep, rest = block.partition("\n")
        first = first.strip()
        if ":" in first and len(first) <= 120:
            head, colon, tail = first.partition(":")
            if colon and len(head.strip()) >= 2:
                sp, pos = _split_label_to_speaker_position(head.strip())
                body = (tail.strip() + ("\n" + rest if rest else "")).strip()
                if body:
                    dialogues.append({"speaker": sp, "position": pos, "text": body})
                    continue
        dialogues.append({"speaker": "", "position": "", "text": block})
    return dialogues if dialogues else [{"speaker": "", "position": "", "text": text}]


def _str_field(obj: dict[str, Any], keys: tuple[str, ...]) -> str:
    for k in keys:
        v = obj.get(k)
        if isinstance(v, str) and v.strip():
            return v.strip()
    return ""


def _normalize_dialogue_row(obj: dict[str, Any]) -> dict[str, str] | None:
    if not isinstance(obj, dict):
        return None
    sp = _str_field(
        obj,
        ("speaker", "name", "Speaker", "speakerName", "speaker_name"),
    )
    pos = _str_field(obj, ("position", "title", "role", "Position"))
    tx = _str_field(
        obj,
        ("text", "dialogue", "speech", "message", "statement", "content"),
    )
    if not tx:
        return None
    return {"speaker": sp, "position": pos, "text": tx}


def _normalize_fmp_transcript_content(content_raw: Any) -> list[dict[str, str]]:
    if isinstance(content_raw, list):
        out: list[dict[str, str]] = []
        for item in content_raw:
            if isinstance(item, dict):
                row = _normalize_dialogue_row(item)
                if row:
                    out.append(row)
            elif isinstance(item, str) and item.strip():
                out.append({"speaker": "", "position": "", "text": item.strip()})
        if out:
            return out
    if isinstance(content_raw, str) and content_raw.strip():
        return _split_plaintext_to_dialogues(content_raw)
    return []


def _fetch_earning_call_transcript_json(
    ticker: str, year: int, quarter: int
) -> Any | None:
    key = _api_key()
    if not key:
        return None
    sym = (ticker or "").strip().upper()
    if not sym or quarter < 1 or quarter > 4:
        return None
    params: dict[str, str | int] = {
        "symbol": sym,
        "year": int(year),
        "quarter": int(quarter),
        "apikey": key,
    }
    url = f"{BASE_URL}/earning-call-transcript"
    try:
        r = requests.get(
            url,
            params=params,
            headers=_REQUEST_HEADERS,
            timeout=_DEFAULT_TIMEOUT_SEC,
        )
        if r.status_code == 402:
            logger.warning(
                "FMP 财报电话逐字稿为付费接口 (HTTP 402 Payment Required)，"
                "当前套餐未包含；电话会分析将回退 earningscall。ticker=%s year=%s quarter=%s",
                sym,
                year,
                quarter,
            )
            return None
        r.raise_for_status()
        return r.json()
    except requests.HTTPError as e:
        code = e.response.status_code if e.response is not None else None
        logger.warning(
            "FMP 逐字稿 HTTP 错误 ticker=%s year=%s quarter=%s status=%s",
            sym,
            year,
            quarter,
            code,
        )
        return None
    except requests.RequestException as e:
        logger.warning(
            "FMP 逐字稿网络错误 ticker=%s year=%s quarter=%s: %s",
            sym,
            year,
            quarter,
            type(e).__name__,
        )
        return None
    except ValueError as e:
        logger.warning(
            "FMP 逐字稿响应非 JSON ticker=%s year=%s quarter=%s: %s",
            sym,
            year,
            quarter,
            e,
        )
        return None


def dialogues_to_plaintext_for_llm(dialogues: list[dict[str, Any]]) -> str:
    """将结构化对话列表拼成带发言人前缀的正文，供分段与 LLM 使用。"""
    parts: list[str] = []
    for b in dialogues:
        sp = str(b.get("speaker") or "").strip()
        pos = str(b.get("position") or "").strip()
        tx = str(b.get("text") or "").strip()
        if not tx:
            continue
        if sp and pos:
            parts.append(f"{sp} ({pos}):\n\n{tx}")
        elif sp:
            parts.append(f"{sp}:\n\n{tx}")
        else:
            parts.append(tx)
    return "\n\n".join(parts)


def get_earnings_transcript(
    ticker: str, year: int, quarter: int
) -> dict[str, Any] | None:
    """
    获取指定季度财报电话会逐字稿（FMP stable ``earning-call-transcript``，参数 ``symbol`` / ``year`` / ``quarter``）。

    返回 ``None`` 表示无数据或请求失败。成功时结构示例::

        {
            "quarter": "2024Q4",
            "date": "2024-10-31",
            "content": [
                {"speaker": "Tim Cook", "text": "..."},
                ...
            ],
        }

    FMP 常将全文放在单个 ``content`` 字符串中；本函数会尽量按「发言人:」拆行并只保留 ``speaker`` + ``text``。
    """
    sym = (ticker or "").strip().upper()
    if not sym or quarter < 1 or quarter > 4:
        return None

    raw = _fetch_earning_call_transcript_json(sym, year, quarter)
    if raw is None:
        return None

    if isinstance(raw, dict) and raw.get("Error Message"):
        logger.warning(
            "FMP 逐字稿错误 ticker=%s: %s",
            sym,
            raw.get("Error Message"),
        )
        return None

    row: dict[str, Any] | None = None
    if isinstance(raw, list) and raw:
        first = raw[0]
        row = first if isinstance(first, dict) else None
    elif isinstance(raw, dict):
        row = raw

    if not row:
        return None

    dialogues = _normalize_fmp_transcript_content(row.get("content"))
    if not dialogues:
        return None

    date_val = row.get("date")
    date_str = str(date_val).strip()[:10] if date_val is not None else ""

    # 对外契约：每条含 speaker + text（position 可选，供 LLM 拼接仍保留）
    content: list[dict[str, str]] = []
    for d in dialogues:
        content.append(
            {
                "speaker": str(d.get("speaker") or "").strip(),
                "text": str(d.get("text") or "").strip(),
            }
        )

    return {
        "quarter": f"{year}Q{quarter}",
        "date": date_str,
        "content": content,
    }


# ---------------------------------------------------------------------------
# Insider trading (stable search)
# ---------------------------------------------------------------------------


def _insider_trade_side(row: dict[str, Any]) -> str:
    """归一为 ``Buy`` / ``Sell`` / ``Other``（优先 ``acquisitionOrDisposition``）。"""
    ad = str(row.get("acquisitionOrDisposition") or "").strip().upper()
    if ad == "A":
        return "Buy"
    if ad == "D":
        return "Sell"
    tt = str(row.get("transactionType") or "").strip().upper()
    if "PURCHASE" in tt or tt.startswith("P-"):
        return "Buy"
    if "SALE" in tt or tt.startswith("S-"):
        return "Sell"
    return "Other"


def _insider_total_value(row: dict[str, Any]) -> float | None:
    v = _num(row.get("totalValue") or row.get("value"))
    if v is not None:
        return v
    sh = _num(row.get("securitiesTransacted"))
    px = _num(row.get("price"))
    if sh is not None and px is not None and px > 0:
        return sh * px
    return None


def get_insider_trades(ticker: str, limit: int = 50) -> list[dict[str, Any]]:
    """
    内部人士交易（FMP stable ``insider-trading/search``；旧路径 ``/insider-trading`` 已 404）。

    返回字典列表，键包含：``transactionDate``、``filingDate``、``insiderName``、
    ``transactionType``（``Buy`` / ``Sell`` / ``Other``）、``shares``、``price``、
    ``totalValue``、``securityName``、``url``、``raw_transaction_type`` 等。
    无密钥、错误或空结果时返回 ``[]``。
    """
    key = _api_key()
    if not key:
        return []
    sym = (ticker or "").strip().upper()
    if not sym:
        return []
    lim = max(1, min(int(limit), 500))
    params: dict[str, str | int] = {
        "symbol": sym,
        "page": 0,
        "limit": lim,
        "apikey": key,
    }
    url = f"{BASE_URL}/insider-trading/search"
    try:
        r = requests.get(
            url,
            params=params,
            headers=_REQUEST_HEADERS,
            timeout=_DEFAULT_TIMEOUT_SEC,
        )
        r.raise_for_status()
        data = r.json()
    except (requests.RequestException, ValueError) as e:
        logger.warning("FMP insider-trading/search 失败 ticker=%s: %s", sym, e)
        return []

    if isinstance(data, dict):
        if data.get("Error Message"):
            logger.warning(
                "FMP insider-trading/search 错误 ticker=%s: %s",
                sym,
                data.get("Error Message"),
            )
            return []
        return []

    if not isinstance(data, list):
        return []

    out: list[dict[str, Any]] = []
    for raw in data:
        if not isinstance(raw, dict):
            continue
        shares = _num(raw.get("securitiesTransacted"))
        price = _num(raw.get("price"))
        tv = _insider_total_value(raw)
        side = _insider_trade_side(raw)
        out.append(
            {
                "symbol": str(raw.get("symbol") or sym).strip().upper(),
                "transactionDate": str(raw.get("transactionDate") or "").strip()[:10],
                "filingDate": str(raw.get("filingDate") or "").strip()[:10],
                "insiderName": str(raw.get("reportingName") or "").strip(),
                "insiderTitle": str(raw.get("typeOfOwner") or "").strip(),
                "transactionType": side,
                "raw_transaction_type": str(raw.get("transactionType") or "").strip(),
                "shares": shares,
                "price": price,
                "totalValue": tv,
                "securitiesOwned": _num(raw.get("securitiesOwned")),
                "securityName": str(raw.get("securityName") or "").strip(),
                "url": str(raw.get("url") or "").strip() or None,
                "formType": str(raw.get("formType") or "").strip() or None,
            }
        )
    return out


# ---------------------------------------------------------------------------
# 季度财务数据（供 Step 6 图表使用）
# ---------------------------------------------------------------------------


def _fetch_statement_quarterly(
    endpoint: str, ticker: str, limit: int
) -> list[dict[str, Any]]:
    key = _api_key()
    if not key:
        return []
    sym = (ticker or "").strip().upper()
    params: dict[str, str | int] = {
        "symbol": sym,
        "apikey": key,
        "limit": int(limit),
        "period": "quarter",
    }
    url = f"{BASE_URL}/{endpoint}"
    try:
        r = requests.get(
            url, params=params, headers=_REQUEST_HEADERS, timeout=_DEFAULT_TIMEOUT_SEC
        )
        r.raise_for_status()
        data = r.json()
    except (requests.RequestException, ValueError) as e:
        logger.warning(
            "FMP quarterly 请求失败 endpoint=%s ticker=%s: %s", endpoint, sym, e
        )
        return []
    if isinstance(data, dict):
        if data.get("Error Message"):
            logger.warning(
                "FMP quarterly 错误 ticker=%s: %s", sym, data.get("Error Message")
            )
            return []
        return []
    if not isinstance(data, list):
        return []
    return [x for x in data if isinstance(x, dict)]


def _quarter_label(
    date_str: str,
    period: str | None = None,
    fiscal_year: str | None = None,
) -> str:
    """
    生成季度标签，优先用 fiscalYear + period（最准确），
    其次用 date 推算，避免财年跨年导致标签错误。
    """
    if fiscal_year and period and "Q" in str(period).upper():
        p = str(period).strip().upper()
        fy = str(fiscal_year).strip()[:4]
        if fy.isdigit():
            return f"{fy}{p}"
    if period and "Q" in str(period).upper():
        p = str(period).strip().upper()
        if date_str and len(date_str) >= 4:
            return f"{date_str[:4]}{p}"
    if not date_str or len(date_str) < 7:
        return date_str or ""
    try:
        from datetime import datetime as _dt

        dt = _dt.strptime(date_str[:10], "%Y-%m-%d")
        q = (dt.month - 1) // 3 + 1
        return f"{dt.year}Q{q}"
    except ValueError:
        return date_str[:7]


def get_quarterly_financials(ticker: str, quarters: int = 6) -> list[dict[str, Any]]:
    """
    获取最近 quarters 个季度的财务指标。
    返回列表按季度降序，每条含：quarter, date, revenue, gross_margin, ebitda, capex, capex_2p_roc。
    ANTI-HALLUCINATION: 字段缺失为 None，禁止估算。
    """
    sym = (ticker or "").strip().upper()
    if not sym or quarters < 1 or not _api_key():
        return []

    lim = max(quarters + 2, 8)
    income_rows = _fetch_statement_quarterly("income-statement", sym, lim)
    if not income_rows:
        return []

    cash_rows = _fetch_statement_quarterly("cash-flow-statement", sym, lim)
    cf_by_date: dict[str, dict[str, Any]] = {}
    for row in cash_rows:
        d = str(row.get("date") or "").strip()[:10]
        if d:
            cf_by_date[d] = row

    raw_records: list[dict[str, Any]] = []
    for inc in income_rows:
        d = str(inc.get("date") or "").strip()[:10]
        if not d:
            continue
        period = str(inc.get("period") or "").strip()
        label = _quarter_label(d, period, fiscal_year=inc.get("fiscalYear"))
        cf = cf_by_date.get(d)
        revenue = _num(inc.get("revenue"))
        gp = _num(inc.get("grossProfit"))
        gross_margin = (gp / revenue) if (revenue and gp is not None) else None
        ebitda = _ebitda(inc, cf)
        capex = _capex(cf)
        raw_records.append(
            {
                "quarter": label,
                "date": d,
                "revenue": revenue,
                "gross_margin": gross_margin,
                "ebitda": ebitda,
                "capex": capex,
                "capex_2p_roc": None,
            }
        )

    raw_records.sort(key=lambda x: x["date"], reverse=True)

    for i, rec in enumerate(raw_records):
        if i + 2 < len(raw_records):
            c_now = rec["capex"]
            c_t2 = raw_records[i + 2]["capex"]
            if c_now is not None and c_t2 is not None and c_t2 != 0:
                rec["capex_2p_roc"] = (c_now - c_t2) / abs(c_t2)

    return raw_records[:quarters]
