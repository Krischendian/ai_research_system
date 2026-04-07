"""财报电话会：逐字稿 + LLM 分析（段落级溯源）。"""
from __future__ import annotations

import json
import re
from datetime import datetime, timezone
from typing import Any

from research_automation.core.database import replace_document_paragraphs
from research_automation.core.paragraph_refs import normalize_paragraph_ref_list
from research_automation.core.paragraph_text import (
    all_paragraph_id_set,
    build_earnings_doc_uid,
    index_to_paragraph_id_map,
    make_earnings_records,
    paragraphs_to_numbered_excerpt,
    split_into_paragraphs,
)
from research_automation.extractors.earnings_call import get_transcript
from research_automation.extractors.llm_client import chat
from research_automation.models.earnings import (
    EarningsCallAnalysis,
    EarningsQuotation,
    EarningsViewpoint,
)


class EarningsAnalysisError(Exception):
    """无法生成电话会分析。"""

    def __init__(self, message: str) -> None:
        self.message = message
        super().__init__(message)


def _extract_json_object(raw: str) -> dict[str, Any]:
    text = raw.strip()
    m = re.search(r"```(?:json)?\s*([\s\S]*?)\s*```", text)
    if m:
        text = m.group(1).strip()
    try:
        data = json.loads(text)
    except json.JSONDecodeError:
        start = text.find("{")
        end = text.rfind("}")
        if start == -1 or end <= start:
            raise
        data = json.loads(text[start : end + 1])
    if not isinstance(data, dict):
        raise ValueError("JSON 根节点须为对象")
    return data


def _build_prompt(symbol: str, quarter: str, numbered_transcript: str) -> str:
    return f"""你是投研助理，根据下面**带段落 ID** 的【财报电话会逐字稿】撰写结构化分析。逐字稿可能为英文，分析输出以中文为主。

**溯源要求**：
- 须使用 JSON 键 **summary_source_paragraph_ids**：字符串数组，每项为 p<序号> 或完整 PARAGRAPH_ID，且须在下文列出的 ID 中存在。
- **management_viewpoints**、**new_business_highlights** 为对象数组，每项含 **text**（中文要点）与 **source_paragraph_ids**（同上）。
- **quotations** 每项除 speaker、quote、topic 外增加 **source_paragraph_ids**（与 quote 相关的段落）。

要求：
1. summary：3～6 句中文概括本季度要点。
2. management_viewpoints：5～8 条管理层核心观点；须能在逐字稿中找到依据。
3. quotations：3～5 条重要原话，quote 保持**英文原文**勿翻译。
4. new_business_highlights：与新产品线、AI、服务、战略动向相关的要点（中文），无则空数组。

仅输出一个 JSON 对象，键名必须为：
summary, summary_source_paragraph_ids, management_viewpoints, quotations, new_business_highlights。

股票代码：{symbol}
季度：{quarter}

【分段逐字稿】
{numbered_transcript}
"""


def _quote_in_transcript(quote: str, transcript: str) -> bool:
    q = (quote or "").strip()
    if not q:
        return False
    if q in transcript:
        return True
    return " ".join(q.split()) in " ".join(transcript.split())


def _parse_viewpoints(
    raw: Any,
    *,
    idx_to_full: dict[int, str],
    allowed: set[str],
) -> list[EarningsViewpoint]:
    if not isinstance(raw, list):
        return []
    out: list[EarningsViewpoint] = []
    for it in raw:
        if isinstance(it, str):
            t = it.strip()
            if t:
                out.append(EarningsViewpoint(text=t, source_paragraph_ids=[]))
            continue
        if not isinstance(it, dict):
            continue
        t = str(it.get("text", "")).strip()
        if not t:
            continue
        ids = normalize_paragraph_ref_list(
            it.get("source_paragraph_ids"),
            idx_to_full=idx_to_full,
            allowed=allowed,
        )
        out.append(EarningsViewpoint(text=t, source_paragraph_ids=ids))
    return out


def _collect_earnings_cited_ids(
    summary_ids: list[str],
    viewpoints: list[EarningsViewpoint],
    quotations: list[EarningsQuotation],
    highlights: list[EarningsViewpoint],
) -> set[str]:
    s: set[str] = set(summary_ids)
    for v in viewpoints:
        s.update(v.source_paragraph_ids)
    for v in highlights:
        s.update(v.source_paragraph_ids)
    for q in quotations:
        s.update(q.source_paragraph_ids)
    return s


def analyze_earnings_call(ticker: str, year: int, quarter: int) -> EarningsCallAnalysis:
    """
    拉取逐字稿，分段入库，经 LLM 生成 ``EarningsCallAnalysis``（含段落溯源）。
    """
    symbol = (ticker or "").strip().upper()
    if not symbol:
        raise EarningsAnalysisError("股票代码不能为空")
    if quarter < 1 or quarter > 4:
        raise EarningsAnalysisError("quarter 必须在 1～4 之间")

    qlabel = f"{year}Q{quarter}"
    transcript = get_transcript(symbol, year, quarter)
    if not transcript.strip():
        raise EarningsAnalysisError("逐字稿为空")

    doc_uid = build_earnings_doc_uid(symbol, year, quarter)
    chunks = split_into_paragraphs(transcript)
    records = make_earnings_records(doc_uid, chunks)
    replace_document_paragraphs(
        doc_uid,
        symbol,
        "EARNINGS_CALL",
        context_year=year,
        quarter_label=qlabel,
        records=[
            (r["paragraph_id"], r["para_index"], r["content"]) for r in records
        ],
    )
    numbered = paragraphs_to_numbered_excerpt(records)
    idx_to_full = index_to_paragraph_id_map(records)
    allowed = all_paragraph_id_set(records)
    id_to_text = {r["paragraph_id"]: r["content"] for r in records}

    prompt = _build_prompt(symbol, qlabel, numbered)
    try:
        reply = chat(
            prompt,
            response_format={"type": "json_object"},
            timeout=120.0,
        )
    except ValueError as e:
        raise EarningsAnalysisError(f"语言模型未就绪：{e}") from e
    except RuntimeError as e:
        raise EarningsAnalysisError(f"调用语言模型失败：{e}") from e

    try:
        payload = _extract_json_object(reply)
    except (json.JSONDecodeError, ValueError) as e:
        raise EarningsAnalysisError(
            "模型返回内容无法解析为 JSON，请稍后重试。"
        ) from e

    summary = str(payload.get("summary", "") or "").strip()
    summary_ids = normalize_paragraph_ref_list(
        payload.get("summary_source_paragraph_ids"),
        idx_to_full=idx_to_full,
        allowed=allowed,
    )

    viewpoints = _parse_viewpoints(
        payload.get("management_viewpoints"),
        idx_to_full=idx_to_full,
        allowed=allowed,
    )

    nbus = _parse_viewpoints(
        payload.get("new_business_highlights"),
        idx_to_full=idx_to_full,
        allowed=allowed,
    )

    quotes_raw = payload.get("quotations")
    quotations: list[EarningsQuotation] = []
    if isinstance(quotes_raw, list):
        for it in quotes_raw:
            if not isinstance(it, dict):
                continue
            q = str(it.get("quote", "")).strip()
            if not q or not _quote_in_transcript(q, transcript):
                continue
            qids = normalize_paragraph_ref_list(
                it.get("source_paragraph_ids"),
                idx_to_full=idx_to_full,
                allowed=allowed,
            )
            quotations.append(
                EarningsQuotation(
                    speaker=str(it.get("speaker", "") or "").strip(),
                    quote=q,
                    topic=str(it.get("topic", "") or "").strip(),
                    source_paragraph_ids=qids,
                )
            )

    cited = _collect_earnings_cited_ids(
        summary_ids, viewpoints, quotations, nbus
    )
    source_paragraphs = {pid: id_to_text[pid] for pid in cited if pid in id_to_text}

    return EarningsCallAnalysis(
        ticker=symbol,
        quarter=qlabel,
        summary=summary,
        summary_source_paragraph_ids=summary_ids,
        management_viewpoints=viewpoints,
        quotations=quotations,
        new_business_highlights=nbus,
        last_updated=datetime.now(timezone.utc).isoformat(),
        data_source_label=(
            "逐字稿来源：项目内 Mock 示例（AAPL 2024Q4 风格）；"
            "Bloomberg 等付费源接口预留于 extractors/earnings_call._fetch_from_bloomberg。"
        ),
        document_uid=doc_uid,
        source_paragraphs=source_paragraphs,
    )
