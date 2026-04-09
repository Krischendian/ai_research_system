"""财报电话会：FMP → EDGAR 8-K → earningscall → sec-api.io；再经 LLM 分析（段落级溯源）。"""
from __future__ import annotations

import json
import logging
import re
from datetime import datetime, timezone
from typing import Any, Literal

from research_automation.core.database import replace_document_paragraphs
from research_automation.core.verbatim_match import quote_matches_haystack
from research_automation.core.paragraph_refs import normalize_paragraph_ref_list
from research_automation.core.paragraph_text import (
    all_paragraph_id_set,
    build_earnings_doc_uid,
    index_to_paragraph_id_map,
    make_earnings_records,
    paragraphs_to_numbered_excerpt,
    split_into_paragraphs,
)
from research_automation.extractors import fmp_client
from research_automation.extractors.earningscall_lib import (
    get_transcript_from_earningscall,
)
from research_automation.extractors.sec_8k_client import (
    fetch_transcript_from_8k,
    search_8k_transcript,
)
from research_automation.extractors.llm_client import chat
from research_automation.models.earnings import (
    EarningsCallAnalysis,
    EarningsQuotation,
    EarningsViewpoint,
)

logger = logging.getLogger(__name__)

# 无逐字稿时 API 返回的说明（与 HTTP detail 一致，便于前端展示）
EARNINGS_NO_TRANSCRIPT_MESSAGE = (
    "No earnings call transcript available：FMP、EDGAR 8-K、earningscall 与 sec-api.io（"
    "SEC_API_KEY）均未返回该季度可用逐字稿；请换季度或检查网络与密钥配置。"
)


class EarningsAnalysisError(Exception):
    """无法生成电话会分析。"""

    def __init__(self, message: str) -> None:
        self.message = message
        super().__init__(message)


def _extract_json_object(raw: str) -> dict[str, Any]:
    """从模型原始字符串中提取 JSON 对象。"""
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
    """拼装 LLM 提示词（含段落溯源要求）。"""
    return f"""你是投研助理，根据下面**带段落 ID** 的【财报电话会逐字稿】撰写结构化分析。逐字稿可能为英文，分析输出以中文为主。

**溯源要求**：
- 须使用 JSON 键 **summary_source_paragraph_ids**：字符串数组，每项为 p<序号> 或完整 PARAGRAPH_ID，且须在下文列出的 ID 中存在。
- **management_viewpoints**、**new_business_highlights** 为对象数组，每项含 **text**（中文要点）与 **source_paragraph_ids**（同上）。
- **quotations** 每项除 speaker、quote、topic 外增加 **source_paragraph_ids**（与 quote 相关的段落）。

要求：
1. summary：3～6 句中文概括本季度要点。
2. management_viewpoints：5～8 条管理层核心观点；须能在逐字稿中找到依据。
3. quotations：5～8 条重要原话，quote 须从逐字稿**原样复制**英文子串（勿改标点与弯引号），勿翻译；**speaker** 须与逐字稿中发言人一致（若正文含「姓名:」或「姓名 (职务):」前缀请对应填写）。
4. new_business_highlights：与新产品线、AI、服务、战略动向相关的要点（中文），无则空数组。

仅输出一个 JSON 对象，键名必须为：
summary, summary_source_paragraph_ids, management_viewpoints, quotations, new_business_highlights。

股票代码：{symbol}
季度：{quarter}

【分段逐字稿】
{numbered_transcript}
"""


def _quote_in_transcript(quote: str, transcript: str) -> bool:
    """判断 quote 是否出现在逐字稿中（逐字归一化 + 模糊匹配）。"""
    return quote_matches_haystack(quote, transcript, similarity_threshold=0.9)


def _parse_viewpoints(
    raw: Any,
    *,
    idx_to_full: dict[int, str],
    allowed: set[str],
) -> list[EarningsViewpoint]:
    """解析管理层观点 / 新业务要点列表。"""
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
    """汇总所有被引用的段落 ID。"""
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
    优先 FMP，其次 EDGAR ``search_8k_transcript``，再 ``earningscall``，最后
    ``fetch_transcript_from_8k``（sec-api.io，需 ``SEC_API_KEY``）；
    将逐字稿分段入库后经 LLM 生成 ``EarningsCallAnalysis``。

    无逐字稿时抛出 ``EarningsAnalysisError``（不再使用 Mock）。
    """
    symbol = (ticker or "").strip().upper()
    if not symbol:
        raise EarningsAnalysisError("股票代码不能为空")
    if quarter < 1 or quarter > 4:
        raise EarningsAnalysisError("quarter 必须在 1～4 之间")

    qlabel = f"{year}Q{quarter}"

    fmp_tr: dict[str, Any] | None = None
    try:
        fmp_tr = fmp_client.get_earnings_transcript(symbol, year, quarter)
    except Exception:
        logger.exception("FMP 逐字稿拉取异常 ticker=%s %s", symbol, qlabel)
        fmp_tr = None

    transcript_origin: Literal["fmp", "sec_8k", "earningscall", "sec_api"]
    transcript = ""

    if fmp_tr and fmp_tr.get("content"):
        dialogues = fmp_tr["content"]
        transcript = fmp_client.dialogues_to_plaintext_for_llm(dialogues).strip()
        transcript_origin = "fmp"
        logger.info("电话会逐字稿来源=FMP ticker=%s %s", symbol, qlabel)
    else:
        sec_text: str | None = None
        try:
            sec_text = search_8k_transcript(
                symbol,
                lookback_days=14,
                fiscal_year=year,
                fiscal_quarter=quarter,
            )
        except Exception:
            logger.exception("EDGAR 8-K 逐字稿拉取异常 ticker=%s %s", symbol, qlabel)
            sec_text = None
        if sec_text and sec_text.strip():
            transcript = sec_text.strip()
            transcript_origin = "sec_8k"
            logger.info("电话会逐字稿来源=SEC_8K_EDGAR ticker=%s %s", symbol, qlabel)
        else:
            transcript = (
                get_transcript_from_earningscall(symbol, year, quarter) or ""
            ).strip()
            if transcript:
                transcript_origin = "earningscall"
                logger.info(
                    "电话会逐字稿来源=earningscall ticker=%s %s", symbol, qlabel
                )
            else:
                api_text: str | None = None
                try:
                    api_text = fetch_transcript_from_8k(
                        symbol,
                        lookback_days=14,
                        fiscal_year=year,
                        fiscal_quarter=quarter,
                    )
                except Exception:
                    logger.exception(
                        "sec-api.io 8-K 逐字稿拉取异常 ticker=%s %s", symbol, qlabel
                    )
                    api_text = None
                if api_text and api_text.strip():
                    transcript = api_text.strip()
                    transcript_origin = "sec_api"
                    logger.info(
                        "电话会逐字稿来源=SEC_API ticker=%s %s", symbol, qlabel
                    )
                else:
                    transcript_origin = "earningscall"
                    logger.info(
                        "电话会无逐字稿 ticker=%s %s（已尝试全部来源）",
                        symbol,
                        qlabel,
                    )

    if not transcript:
        logger.warning("无逐字稿 ticker=%s %s", symbol, qlabel)
        raise EarningsAnalysisError(EARNINGS_NO_TRANSCRIPT_MESSAGE)

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

    if transcript_origin == "fmp":
        ds_label = (
            "逐字稿来源：Financial Modeling Prep (FMP) earning-call-transcript API；"
            "分析由本地 LLM 基于该文本生成。"
        )
    elif transcript_origin == "sec_8k":
        ds_label = (
            "逐字稿来源：SEC EDGAR Form 8-K 附件（常见 EX-99.1 业绩说明/电话会文稿）；"
            "分析由本地 LLM 基于该文本生成。"
        )
    elif transcript_origin == "sec_api":
        ds_label = (
            "逐字稿来源：sec-api.io Full-Text Search（8-K EX-99 等附件 URL 拉取）；"
            "分析由本地 LLM 基于该文本生成。"
        )
    else:
        ds_label = (
            "逐字稿来源：earningscall 库（公开财报电话会文本）；"
            "分析由本地 LLM 基于该文本生成。"
        )

    return EarningsCallAnalysis(
        ticker=symbol,
        quarter=qlabel,
        summary=summary,
        summary_source_paragraph_ids=summary_ids,
        management_viewpoints=viewpoints,
        quotations=quotations,
        new_business_highlights=nbus,
        last_updated=datetime.now(timezone.utc).isoformat(),
        data_source=transcript_origin,
        data_source_label=ds_label,
        document_uid=doc_uid,
        source_paragraphs=source_paragraphs,
    )
