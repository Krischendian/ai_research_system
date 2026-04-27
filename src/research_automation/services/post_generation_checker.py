from __future__ import annotations

import logging
import re
from typing import Any

from .checker_config import (
    DEFAULT_TOLERANCE,
    METRIC_KEYWORDS,
    TICKER_KEYWORDS,
    WARNING_THRESHOLD,
)

logger = logging.getLogger(__name__)


_NUMBER_PATTERN = re.compile(
    r"""
    (?P<usd>\$\s*[+-]?\d+(?:,\d{3})*(?:\.\d+)?\s*(?:[BbMmKk]|billion|million|thousand)?)
    |
    (?P<cn>[+-]?\d+(?:\.\d+)?\s*亿(?:美元)?)
    |
    (?P<pct>[+-]?\d+(?:\.\d+)?\s*%)
    |
    (?P<plain_usd>[+-]?\d+(?:\.\d+)?\s*美元)
    """,
    re.VERBOSE | re.IGNORECASE,
)


def _sentence_spans(text: str) -> list[tuple[int, int, str]]:
    spans: list[tuple[int, int, str]] = []
    start = 0
    for m in re.finditer(r"[。！？!?；;\n]", text):
        end = m.end()
        sentence = text[start:end].strip()
        if sentence:
            spans.append((start, end, sentence))
        start = end
    if start < len(text):
        tail = text[start:].strip()
        if tail:
            spans.append((start, len(text), tail))
    return spans


def _find_sentence_for_pos(text: str, pos: int) -> str:
    for s_start, s_end, sentence in _sentence_spans(text):
        if s_start <= pos < s_end:
            return sentence
    return text


def extract_numbers(text: str) -> list[dict[str, Any]]:
    out: list[dict[str, Any]] = []
    if not text:
        return out
    for m in _NUMBER_PATTERN.finditer(text):
        raw = m.group(0).strip()
        if not raw:
            continue
        start = m.start()
        out.append(
            {
                "raw": raw,
                "sentence": _find_sentence_for_pos(text, start),
                "char_start": start,
            }
        )
    return out


def attribute_number(extracted: dict[str, Any]) -> dict[str, Any]:
    sentence = str(extracted.get("sentence") or "")
    sentence_lower = sentence.lower()

    ticker: str | None = None
    metric: str | None = None

    for tk, kws in TICKER_KEYWORDS.items():
        if any((kw or "").lower() in sentence_lower for kw in kws):
            ticker = tk
            break

    for mk, kws in METRIC_KEYWORDS.items():
        if any((kw or "").lower() in sentence_lower for kw in kws):
            metric = mk
            break

    enriched = dict(extracted)
    enriched["ticker"] = ticker
    enriched["metric"] = metric
    year_match = re.search(r"\b(20\d{2})\b", sentence)
    enriched["year"] = int(year_match.group(1)) if year_match else None
    return enriched


def normalize_to_billion(raw: str) -> float | None:
    if not raw:
        return None
    s = raw.strip()
    s_clean = s.replace(",", "").replace(" ", "")

    try:
        if "%" in s_clean:
            num_match = re.search(r"[+-]?\d+(?:\.\d+)?", s_clean)
            if not num_match:
                return None
            return float(num_match.group(0)) / 100.0

        if "亿" in s_clean:
            num_match = re.search(r"[+-]?\d+(?:\.\d+)?", s_clean)
            if not num_match:
                return None
            return float(num_match.group(0)) / 10.0

        if "$" in s or "美元" in s:
            unit_match = re.search(r"(billion|million|thousand|[BbMmKk])\b", s, re.I)
            num_match = re.search(r"[+-]?\d+(?:\.\d+)?", s_clean)
            if not num_match:
                return None
            val = float(num_match.group(0))
            if not unit_match:
                return val
            unit = unit_match.group(1).lower()
            if unit in ("b", "billion"):
                return val
            if unit in ("m", "million"):
                return val / 1000.0
            if unit in ("k", "thousand"):
                return val / 1_000_000.0
            return None
    except Exception:
        return None
    return None


def compare_with_baseline(
    attributed: dict[str, Any],
    baseline: dict[str, Any],
    tolerance: float = DEFAULT_TOLERANCE,
) -> dict[str, Any]:
    result = dict(attributed)
    raw = str(attributed.get("raw") or "")
    ticker = attributed.get("ticker")
    metric = attributed.get("metric")

    result["normalized_value"] = normalize_to_billion(raw)
    result["baseline_value"] = None
    result["diff_pct"] = None
    result["reason"] = None

    if ticker is None or metric is None:
        result["status"] = "🔵"
        return result
    if metric == "yoy_growth":
        result["status"] = "🔵"
        result["reason"] = "同比增速无年报基线可比对"
        return result

    company_data = baseline.get(ticker)
    if not company_data:
        result["status"] = "🔵"
        return result

    norm_val = result["normalized_value"]
    if norm_val is None:
        result["status"] = "🔵"
        return result

    year = attributed.get("year")
    selected_year: int | None = None
    if isinstance(year, int) and year in company_data:
        selected_year = year
    else:
        years = [y for y in company_data.keys() if isinstance(y, int)]
        if years:
            selected_year = max(years)
    if selected_year is None:
        result["status"] = "🔵"
        return result

    metric_base = company_data.get(selected_year, {}).get(metric)
    if metric_base is None:
        result["status"] = "🔵"
        return result
    baseline_val = float(metric_base)
    if baseline_val == 0:
        result["status"] = "🔵"
        result["reason"] = "基线值缺失或为零，无法核实"
        return result

    result["baseline_value"] = baseline_val
    denom = abs(baseline_val)
    if denom == 0:
        diff_pct = abs(float(norm_val) - baseline_val)
    else:
        diff_pct = abs(float(norm_val) - baseline_val) / denom
    result["diff_pct"] = diff_pct

    if diff_pct <= tolerance:
        result["status"] = "✅"
        return result
    if diff_pct <= WARNING_THRESHOLD:
        result["status"] = "⚠️"
        result["reason"] = f"偏差 {diff_pct:.1%} 超过容差 {tolerance:.1%}"
        return result
    result["status"] = "🔴"
    result["reason"] = f"偏差 {diff_pct:.1%}，超过错误阈值 {WARNING_THRESHOLD:.0%}"
    return result


def _annotation_suffix(result: dict[str, Any]) -> str:
    status = result.get("status")
    raw = str(result.get("raw") or "")
    if status == "⚠️":
        baseline_val = result.get("baseline_value")
        diff_pct = result.get("diff_pct")
        if baseline_val is None or diff_pct is None:
            return f"{raw}[⚠️]"
        return f"{raw}[⚠️ 基线:{baseline_val:.3g}B, 偏差:{diff_pct:.1%}]"
    if status == "🔴":
        baseline_val = result.get("baseline_value")
        if baseline_val is None:
            return f"~~{raw}~~[🔴 疑似错误，待人工复核]"
        return f"~~{raw}~~[🔴 疑似错误，基线:{baseline_val:.3g}B，待人工复核]"
    if status == "🔵":
        return f"{raw}[🔵]"
    return raw


def annotate_paragraph(
    paragraph: str,
    baseline: dict[str, Any],
    quarterly_mode: bool = False,
) -> tuple[str, list[dict[str, Any]]]:
    extracted = extract_numbers(paragraph)
    findings: list[dict[str, Any]] = []
    if not extracted:
        return paragraph, findings

    enriched: list[dict[str, Any]] = []
    for item in extracted:
        attributed = attribute_number(item)
        if quarterly_mode:
            compared = {
                **attributed,
                "status": "🔵",
                "reason": "季报段落，不与年报基线比对",
                "normalized_value": normalize_to_billion(str(attributed.get("raw") or "")),
                "baseline_value": None,
                "diff_pct": None,
            }
        else:
            compared = compare_with_baseline(attributed, baseline)
        findings.append(compared)
        enriched.append(compared)

    # 仅保留检查结果，不在正文中插入任何内联标注。
    # 这里仍保留 extract/attribute/compare 全流程，供 findings/summary 使用。
    return paragraph, findings


def build_baseline_from_rows(ticker: str, rows: list[Any]) -> dict[str, dict[int, dict[str, Any]]]:
    """
    把 _get_validated_financials 返回的 rows (list[AnnualFinancials])
    转成 baseline dict，单位统一转为 B（除以 1e9）
    """
    result: dict[str, dict[int, dict[str, Any]]] = {ticker: {}}
    for row in rows or []:
        d = row.model_dump()
        year = d.get("year")
        if not isinstance(year, int):
            continue
        result[ticker][year] = {
            "revenue": (d.get("revenue") or 0) / 1e9,
            "net_income": (d.get("net_income") or 0) / 1e9,
            "ebitda": (d.get("ebitda") or 0) / 1e9,
            "capex": (d.get("capex") or 0) / 1e9,
            "gross_margin": d.get("gross_margin"),
            "net_debt_to_equity": d.get("net_debt_to_equity"),
        }
    return result


def run_post_generation_check(texts: dict[str, Any], baseline: dict[str, Any]) -> dict[str, Any]:
    original = texts or {}
    annotated: dict[str, Any] = {}
    all_findings: list[dict[str, Any]] = []

    summary = {
        "total": 0,
        "passed": 0,
        "warning": 0,
        "error": 0,
        "unverified": 0,
    }

    try:
        for key, value in original.items():
            if isinstance(value, str):
                quarterly_mode = key in {"step4_sector_summary"}
                marked, findings = annotate_paragraph(
                    value, baseline, quarterly_mode=quarterly_mode
                )
                annotated[key] = marked
                all_findings.extend(findings)
            elif isinstance(value, dict):
                sub_annotated: dict[str, str] = {}
                for sub_key, sub_text in value.items():
                    if isinstance(sub_text, str):
                        quarterly_mode = key in {"step4_companies"}
                        marked, findings = annotate_paragraph(
                            sub_text, baseline, quarterly_mode=quarterly_mode
                        )
                        sub_annotated[sub_key] = marked
                        all_findings.extend(findings)
                    else:
                        sub_annotated[sub_key] = sub_text
                annotated[key] = sub_annotated
            else:
                annotated[key] = value

        for item in all_findings:
            status = item.get("status")
            summary["total"] += 1
            if status == "✅":
                summary["passed"] += 1
            elif status == "⚠️":
                summary["warning"] += 1
            elif status == "🔴":
                summary["error"] += 1
            else:
                summary["unverified"] += 1

        flagged = [f for f in all_findings if f.get("status") in ("⚠️", "🔴")]
        return {
            "annotated": annotated,
            "summary": summary,
            "findings": flagged,
        }
    except Exception:
        logger.exception("post generation checker failed")
        return {
            "annotated": original,
            "summary": summary,
            "findings": [],
        }
