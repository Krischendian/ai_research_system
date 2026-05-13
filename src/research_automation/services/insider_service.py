"""内部人士交易：FMP ``insider-trading/search`` 汇总；FMP无数据时回退 SEC EDGAR Form 4。"""
from __future__ import annotations

import json
import logging
import re
from collections import Counter
from datetime import date, datetime, timedelta
from typing import Any

import requests

from research_automation.extractors import fmp_client

logger = logging.getLogger(__name__)

# ── SEC EDGAR Form 4 回退 ──────────────────────────────────────────────────────

_SEC_HEADERS = {"User-Agent": "research-automation/1.0 (internal use)"}
_SEC_TIMEOUT = 15


def _get_sec_user_agent() -> str:
    import os
    ua = os.getenv("SEC_EDGAR_USER_AGENT", "").strip()
    return ua if ua else "research-automation/1.0 (contact@example.com)"


def _get_cik_for_ticker(ticker: str) -> str | None:
    """通过 SEC EDGAR company_tickers.json 获取 CIK（10位补零字符串）。"""
    try:
        r = requests.get(
            "https://www.sec.gov/files/company_tickers.json",
            headers={"User-Agent": _get_sec_user_agent()},
            timeout=_SEC_TIMEOUT,
        )
        r.raise_for_status()
        data = r.json()
    except Exception as e:
        logger.warning("SEC company_tickers.json 获取失败: %s", e)
        return None
    # data: { "0": {"cik_str": 123, "ticker": "AAPL", ...}, ... }
    sym_upper = ticker.strip().upper()
    for item in data.values():
        if isinstance(item, dict) and str(item.get("ticker", "")).upper() == sym_upper:
            cik = str(item.get("cik_str", "")).strip()
            return cik.zfill(10) if cik else None
    return None


def _parse_form4_xml(xml_text: str, ticker: str) -> list[dict[str, Any]]:
    """
    从 Form 4 XML 解析出交易条目。
    返回与 FMP 格式兼容的字典列表（含 transactionDate, insiderName, transactionType, shares, price, totalValue）。
    """
    trades: list[dict[str, Any]] = []
    try:
        # 提取 reportingOwner
        name_m = re.search(r"<rptOwnerName>(.*?)</rptOwnerName>", xml_text, re.DOTALL)
        title_m = re.search(r"<officerTitle>(.*?)</officerTitle>", xml_text, re.DOTALL)
        insider_name = name_m.group(1).strip() if name_m else "UNKNOWN"
        insider_title = title_m.group(1).strip() if title_m else ""

        # 提取 nonDerivativeTransaction 交易明细
        for block in re.findall(
            r"<nonDerivativeTransaction>(.*?)</nonDerivativeTransaction>", xml_text, re.DOTALL
        ):
            date_m = re.search(r"<transactionDate>.*?<value>(.*?)</value>", block, re.DOTALL)
            code_m = re.search(r"<transactionCode>(.*?)</transactionCode>", block, re.DOTALL)
            shares_m = re.search(r"<transactionShares>.*?<value>(.*?)</value>", block, re.DOTALL)
            price_m = re.search(r"<transactionPricePerShare>.*?<value>(.*?)</value>", block, re.DOTALL)
            ad_m = re.search(r"<transactionAcquiredDisposedCode>.*?<value>(.*?)</value>", block, re.DOTALL)

            tx_date = date_m.group(1).strip()[:10] if date_m else ""
            tx_code = code_m.group(1).strip().upper() if code_m else ""
            ad_code = ad_m.group(1).strip().upper() if ad_m else ""
            try:
                shares = float(shares_m.group(1).strip()) if shares_m else None
            except ValueError:
                shares = None
            try:
                price = float(price_m.group(1).strip()) if price_m else None
            except ValueError:
                price = None

            # 判断买卖方向：A=acquisition(Buy), D=disposition(Sell)
            if ad_code == "A" or tx_code in ("P",):
                side = "Buy"
            elif ad_code == "D" or tx_code in ("S", "F"):
                side = "Sell"
            else:
                side = "Other"

            total_value = None
            if shares is not None and price is not None and price > 0:
                total_value = shares * price

            trades.append({
                "symbol": ticker,
                "transactionDate": tx_date,
                "filingDate": tx_date,
                "insiderName": insider_name,
                "insiderTitle": insider_title,
                "transactionType": side,
                "raw_transaction_type": tx_code,
                "shares": shares,
                "price": price,
                "totalValue": total_value,
                "securitiesOwned": None,
                "securityName": "Common Stock",
                "url": None,
                "source": "SEC_EDGAR_Form4",
            })
    except Exception as e:
        logger.warning("Form 4 XML 解析失败: %s", e)
    return trades


def get_insider_trades_sec_form4(ticker: str, days_back: int = 30) -> list[dict[str, Any]]:
    """
    通过 SEC EDGAR submissions API 拉取最近 Form 4 申报，解析并返回与 FMP 格式兼容的交易列表。
    非美股（含 GY / LN 后缀）直接返回 []，因为 SEC 无此类数据。
    """
    # 非美股跳过（SEC 只有美股 Form 4）
    sym_clean = ticker.strip().upper()
    if any(sym_clean.endswith(sfx) for sfx in (" GY", " LN", " FP", " JP", ".DE", ".L", ".PA")):
        return []
    # 去掉 " US" 后缀取纯代码
    base_sym = re.sub(r"\s+(US|UN|UW|UA|UT)$", "", sym_clean).strip()

    cik = _get_cik_for_ticker(base_sym)
    if not cik:
        logger.info("SEC Form 4 回退：找不到 CIK ticker=%s", base_sym)
        return []

    # 拉取 submissions JSON
    try:
        url = f"https://data.sec.gov/submissions/CIK{cik}.json"
        r = requests.get(url, headers={"User-Agent": _get_sec_user_agent()}, timeout=_SEC_TIMEOUT)
        r.raise_for_status()
        submissions = r.json()
    except Exception as e:
        logger.warning("SEC submissions 获取失败 cik=%s: %s", cik, e)
        return []

    recent = submissions.get("filings", {}).get("recent", {})
    forms = recent.get("form", [])
    dates = recent.get("filingDate", [])
    accs = recent.get("accessionNumber", [])
    primary_docs = recent.get("primaryDocument", [])

    cutoff = date.today() - timedelta(days=max(1, int(days_back)))
    form4_filings: list[tuple[str, str, str]] = []  # (accession, primaryDoc, filingDate)

    for i, form in enumerate(forms):
        if str(form).strip() not in ("4", "4/A"):
            continue
        fd_str = str(dates[i])[:10] if i < len(dates) else ""
        try:
            fd = datetime.strptime(fd_str, "%Y-%m-%d").date()
        except ValueError:
            continue
        if fd < cutoff:
            continue
        acc = str(accs[i]).replace("-", "") if i < len(accs) else ""
        doc = str(primary_docs[i]) if i < len(primary_docs) else ""
        form4_filings.append((acc, doc, fd_str))
        if len(form4_filings) >= 20:  # 限制最多20份，避免请求过多
            break

    if not form4_filings:
        return []

    all_trades: list[dict[str, Any]] = []
    for acc, doc, fd_str in form4_filings:
        if not acc or not doc:
            continue
        acc_fmt = f"{acc[:10]}-{acc[10:12]}-{acc[12:]}"
        # 去掉 xslF345X06/ 等 XSL 渲染前缀，拿原始 XML
        doc_clean = doc.split("/")[-1] if "/" in doc else doc
        xml_url = f"https://www.sec.gov/Archives/edgar/full-index/{fd_str[:4]}/{fd_str[5:7]}/{acc_fmt}/{doc_clean}"
        # 尝试直接 accession 路径
        xml_url_alt = f"https://www.sec.gov/Archives/edgar/data/{int(cik)}/{acc}/{doc_clean}"
        for url in [xml_url_alt, xml_url]:
            try:
                resp = requests.get(
                    url, headers={"User-Agent": _get_sec_user_agent()}, timeout=_SEC_TIMEOUT
                )
                if resp.status_code == 200 and resp.text.strip().startswith("<?xml"):
                    parsed = _parse_form4_xml(resp.text, base_sym)
                    # 补充 filingDate
                    for t in parsed:
                        if not t.get("filingDate"):
                            t["filingDate"] = fd_str
                    all_trades.extend(parsed)
                    break
            except Exception as e:
                logger.debug("Form 4 XML 获取失败 url=%s: %s", url, e)
                continue

    logger.info("SEC Form 4 回退：ticker=%s 获取到 %d 条交易", base_sym, len(all_trades))
    return all_trades


def _parse_iso_date(s: str | None) -> date | None:
    if not s:
        return None
    raw = str(s).strip()[:10]
    if len(raw) < 10:
        return None
    try:
        return datetime.strptime(raw, "%Y-%m-%d").date()
    except ValueError:
        return None


def _floaty(v: object) -> float | None:
    """安全转 ``float``；无效或 NaN 返回 ``None``。"""
    if v is None:
        return None
    try:
        x = float(v)  # type: ignore[arg-type]
    except (TypeError, ValueError):
        return None
    if x != x:  # NaN
        return None
    return x


def _notional_for_trade(r: dict[str, Any]) -> float | None:
    """
    单笔名义金额：优先 ``totalValue``；缺失时用 ``shares * price``。
    仍无法得到有效数值时返回 ``None``（展示层可写「股数未披露」）。
    """
    tv = _floaty(r.get("totalValue"))
    if tv is not None and tv > 0:
        return tv
    sh = _floaty(r.get("shares"))
    px = _floaty(r.get("price"))
    if sh is not None and px is not None and sh > 0 and px > 0:
        return sh * px
    if tv is not None and tv == 0:
        return 0.0
    return None


def get_insider_summary(ticker: str, days_back: int = 30) -> dict[str, Any]:
    """
    拉取内部交易并筛选 ``transactionDate``（缺省则用 ``filingDate``）在
    最近 ``days_back`` 日内的记录，统计买卖笔数与名义金额、主要内部人士。

    金额优先 ``totalValue``，否则用股数×成交价推算；若该侧有成交但全部无法推算金额，
    则 ``total_buy_value`` / ``total_sell_value`` 为 ``None``，由报告写「金额未披露」。

    **单位约定**（三条路径必须保持一致）：
    - ``total_buy_value`` / ``total_sell_value`` / ``net_value`` / ``top_insiders[*].total_value``
      统一为**美元（USD）裸数字**，非千美元或百万美元。
    - Bloomberg 路径：``shares_bought × close_price`` ⇒ 股数 × USD/股 = USD。
    - FMP / SEC Form 4 路径：优先 ``totalValue``（Form 4 标准为 USD），缺失时
      用 ``shares × price`` 推算（亦 USD）；RSU/Phantom Stock 归属或行权释放
      （``price=0`` 且 ``totalValue=None``）会被识别为"无可统计 notional"忽略，
      不汇入买卖金额，避免错把奖励股数等比当成市场交易额。
    """
    sym = (ticker or "").strip().upper()
    if not sym:
        return {
            "ticker": "",
            "days_back": days_back,
            "trade_count": 0,
            "buy_count": 0,
            "sell_count": 0,
            "other_count": 0,
            "total_buy_value": None,
            "total_sell_value": None,
            "net_value": None,
            "top_insiders": [],
            "trades": [],
        }

    _bbg_attempted = False
    # Bloomberg 优先：有月度数据直接汇总
    try:
        from research_automation.extractors.bloomberg_reader import get_insider_monthly

        bbg_rows = get_insider_monthly(sym, months=12)
        _bbg_attempted = True
        if bbg_rows:
            # 只取最近 days_back 天内的月份
            cutoff_bbg = date.today() - timedelta(days=max(1, int(days_back)))
            buy_c = sell_c = 0
            buy_val = sell_val = 0.0
            buy_any = sell_any = False
            for r in bbg_rows:
                # month 格式 "MM/YYYY" → date
                try:
                    mm, yyyy = r["month"].split("/")
                    row_date = date(int(yyyy), int(mm), 1)
                except Exception:
                    continue
                if row_date < cutoff_bbg:
                    continue
                bought = float(r["shares_bought"] or 0)
                sold = abs(float(r["shares_sold"] or 0))
                price = float(r["close_price"] or 0)
                if bought > 0:
                    buy_c += 1
                    if price > 0:
                        buy_any = True
                        buy_val += bought * price
                if sold > 0:
                    sell_c += 1
                    if price > 0:
                        sell_any = True
                        sell_val += sold * price
            if buy_c > 0 or sell_c > 0:
                net_value = None
                if buy_any and sell_any:
                    net_value = buy_val - sell_val
                elif buy_any:
                    net_value = buy_val
                elif sell_any:
                    net_value = -sell_val
                return {
                    "ticker": sym,
                    "days_back": int(days_back),
                    "trade_count": buy_c + sell_c,
                    "buy_count": buy_c,
                    "sell_count": sell_c,
                    "other_count": 0,
                    "total_buy_value": buy_val if buy_any else None,
                    "total_sell_value": sell_val if sell_any else None,
                    "net_value": net_value,
                    "top_insiders": [],  # Bloomberg 月度数据无个人级别
                    "trades": [],
                    "source": "Bloomberg INSIDER_MONTHLY_TRANSACTION",
                }
    except Exception as _bbg_err:
        logger.debug("Bloomberg insider fallback: %s", _bbg_err)

    try:
        rows = fmp_client.get_insider_trades(sym, limit=max(50, days_back * 3))
    except Exception:
        logger.exception("内部交易拉取异常 ticker=%s", sym)
        rows = []

    used_sec_form4 = False
    # FMP 无数据时，回退到 SEC EDGAR Form 4
    if not rows:
        logger.info("FMP insider 无数据，尝试 SEC Form 4 回退 ticker=%s", sym)
        try:
            rows = get_insider_trades_sec_form4(sym, days_back=days_back)
            used_sec_form4 = True
        except Exception:
            logger.exception("SEC Form 4 回退异常 ticker=%s", sym)
            rows = []

    cutoff = date.today() - timedelta(days=max(1, int(days_back)))
    filtered: list[dict[str, Any]] = []
    for r in rows:
        td = _parse_iso_date(str(r.get("transactionDate") or ""))
        if td is None:
            td = _parse_iso_date(str(r.get("filingDate") or ""))
        if td is None or td < cutoff:
            continue
        filtered.append(r)

    buy_c = sell_c = other_c = 0
    buy_val = 0.0
    sell_val = 0.0
    buy_any = sell_any = False
    insider_values: dict[str, float] = {}
    insider_counts: Counter[str] = Counter()
    insider_has_notional: dict[str, bool] = {}

    for r in filtered:
        side = str(r.get("transactionType") or "").strip()
        name = str(r.get("insiderName") or "").strip() or "UNKNOWN"
        n = _notional_for_trade(r)

        if side == "Buy":
            buy_c += 1
            if n is not None:
                buy_any = True
                buy_val += float(n)
        elif side == "Sell":
            sell_c += 1
            if n is not None:
                sell_any = True
                sell_val += float(n)
        else:
            other_c += 1

        insider_counts[name] += 1
        if n is not None:
            insider_has_notional[name] = True
            insider_values[name] = insider_values.get(name, 0.0) + float(n)

    top: list[dict[str, Any]] = []
    for name, cnt in insider_counts.most_common(8):
        has_n = insider_has_notional.get(name, False)
        tv = insider_values.get(name) if has_n else None
        top.append(
            {
                "insiderName": name,
                "trades": int(cnt),
                "total_value": float(tv) if tv is not None else None,
            }
        )

    total_buy_value = buy_val if buy_any else (None if buy_c > 0 else None)
    total_sell_value = sell_val if sell_any else (None if sell_c > 0 else None)
    # 任一侧有笔数但金额全部不可算时，净买卖不展示，避免误导读数
    net_value = None
    if (buy_c > 0 and not buy_any) or (sell_c > 0 and not sell_any):
        net_value = None
    elif buy_any or sell_any:
        net_value = (total_buy_value or 0.0) - (total_sell_value or 0.0)

    return {
        "ticker": sym,
        "days_back": int(days_back),
        "trade_count": len(filtered),
        "buy_count": buy_c,
        "sell_count": sell_c,
        "other_count": other_c,
        "total_buy_value": total_buy_value,
        "total_sell_value": total_sell_value,
        "net_value": net_value,
        "top_insiders": top,
        "trades": filtered,
        "source": (
            "Bloomberg（窗口内无交易）→ FMP（无数据）→ SEC Form 4"
            if used_sec_form4 and _bbg_attempted
            else (
                "SEC Form 4"
                if used_sec_form4
                else ("Bloomberg（窗口内无交易）→ FMP" if _bbg_attempted else "FMP")
            )
        ),
    }