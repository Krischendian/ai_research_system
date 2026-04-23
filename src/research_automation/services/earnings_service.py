"""财报电话会：FMP → EDGAR 8-K → sec-api.io；再经 LLM 分析（段落级溯源）。"""
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
# from research_automation.extractors.earningscall_lib import (
#     get_transcript_from_earningscall,
# )
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
    "No earnings call transcript available：FMP、EDGAR 8-K 与 sec-api.io（"
    "SEC_API_KEY）均未返回该季度可用逐字稿；请换季度或检查网络与密钥配置。"
)


class EarningsAnalysisError(Exception):
    """无法生成电话会分析。"""

    def __init__(self, message: str) -> None:
        self.message = message
        super().__init__(message)


def _extract_json_object(raw: str) -> dict[str, Any]:
    """从模型原始字符串中提取 JSON 对象，健壮处理 Claude 常见输出问题。"""
    text = raw.strip()

    # 1. 去掉 markdown 代码块
    m = re.search(r"```(?:json)?\s*([\s\S]*?)\s*```", text)
    if m:
        text = m.group(1).strip()

    # 2. 先尝试直接解析
    try:
        return json.loads(text)
    except json.JSONDecodeError:
        pass

    # 3. 提取首个 {...} 块
    start = text.find("{")
    end = text.rfind("}")
    if start != -1 and end > start:
        chunk = text[start: end + 1]
        try:
            return json.loads(chunk)
        except json.JSONDecodeError:
            pass

        # 4. 弯引号处理：只在 JSON 字符串值内替换，用 \" 转义
        # 策略：把弯引号先变成占位符，parse 后再还原
        cleaned = chunk
        # 把弯左/右引号替换为转义双引号
        cleaned = re.sub(r"[\u201c\u201d]", '\\"', cleaned)
        cleaned = cleaned.replace("\u2018", "'").replace("\u2019", "'")
        try:
            return json.loads(cleaned)
        except json.JSONDecodeError:
            pass

        # 5. 最后兜底：强制用 ast
        try:
            import ast
            result = ast.literal_eval(chunk)
            if isinstance(result, dict):
                return result
        except Exception:
            pass

    raise json.JSONDecodeError("无法解析 JSON", text, 0)


def _build_prompt(
    symbol: str,
    quarter: str,
    numbered_transcript: str,
    sector_watch_items: list[str] | None = None,
) -> str:
    """拼装 LLM 提示词（含段落溯源要求）。"""
    # 防止 prompt 过长导致输出截断：超过 40000 字符时截取前 40000 字符
    MAX_TRANSCRIPT_CHARS = 40000
    numbered = numbered_transcript
    if len(numbered) > MAX_TRANSCRIPT_CHARS:
        numbered = numbered[:MAX_TRANSCRIPT_CHARS] + "\n\n[逐字稿过长，已截取前40000字符]"

    watch_block = ""
    if sector_watch_items:
        items = "；".join(
            str(x).strip() for x in sector_watch_items if str(x).strip()
        )
        if items:
            watch_block = f"""
**本 sector 关注项**（请在概括、管理层观点、原话与新业务要点中尽量覆盖；仍须严格基于逐字稿段落 ID 溯源，禁止臆造）：
{items}
"""
    return f"""你是投研助理，根据下面**带段落 ID** 的【财报电话会逐字稿】撰写结构化分析。逐字稿可能为英文，分析输出以中文为主。
{watch_block}
**溯源要求**：
- 须使用 JSON 键 **summary_source_paragraph_ids**：字符串数组，每项为 p<序号> 或完整 PARAGRAPH_ID，且须在下文列出的 ID 中存在。
- **management_viewpoints**、**new_business_highlights** 为对象数组，每项含 **text**（中文要点）与 **source_paragraph_ids**（同上）。
- **quotations** 每项除 speaker、quote、topic 外增加 **source_paragraph_ids**（与 quote 相关的段落）。

要求（严格执行，每条都必须满足数量要求）：
1. summary：3～6 句中文概括，必须包含具体数字（收入、增长率、订单额等）。

2. management_viewpoints：**必须返回至少6条**，每条须：
   - 包含具体数字或可验证事实（如"$18B revenue"、"4% growth"）
   - 标注 source_paragraph_ids
   - 不得重复相同主题

3. quotations：**必须返回至少5条**，每条须：
   - quote 字段：从逐字稿**一字不差**复制连续英文原文（至少15个词），禁止缩写或改写
   - speaker 字段：与逐字稿发言人完全一致（如"Julie T. Sweet"、"Angie Park"）
   - topic 字段：1-3个英文词的主题标签
   - 优先选取含具体数字、指引、或与 sector_watch_items 相关的原话
   - 不同发言人的原话都要覆盖（CEO、CFO、分析师问答均要有）

4. new_business_highlights：与新产品线、AI、收购、战略投资相关的要点（中文），**至少3条**，无则空数组。

⚠️ 如果 quotations 少于5条，你的回答将被视为不合格。请仔细阅读逐字稿找出足够的原话。

仅输出一个 JSON 对象，键名必须为：
summary, summary_source_paragraph_ids, management_viewpoints, quotations, new_business_highlights。

股票代码：{symbol}
季度：{quarter}

【分段逐字稿】
{numbered}

重要：直接输出 JSON 对象，第一个字符必须是 {{，最后一个字符必须是 }}，不要任何解释、不要 markdown 代码块。
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


def analyze_earnings_call(
    ticker: str,
    year: int,
    quarter: int,
    *,
    sector_watch_items: list[str] | None = None,
) -> EarningsCallAnalysis:
    """
    优先 FMP，其次 EDGAR ``search_8k_transcript``，再
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

    transcript_origin: Literal["fmp", "sec_8k", "sec_api"]
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
                transcript_origin = "sec_api"
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

    prompt = _build_prompt(symbol, qlabel, numbered, sector_watch_items)
    try:
        reply = chat(
            prompt,
            response_format={"type": "json_object"},
            timeout=180.0,
            max_tokens=6000,
        )
    except ValueError as e:
        raise EarningsAnalysisError(f"语言模型未就绪：{e}") from e
    except RuntimeError as e:
        raise EarningsAnalysisError(f"调用语言模型失败：{e}") from e

    try:
        payload = _extract_json_object(reply)
    except (json.JSONDecodeError, ValueError):
        # JSON解析失败时重试一次（Claude偶发返回不完整JSON）
        logger.warning("Step4 JSON解析失败，重试一次 ticker=%s", symbol)
        try:
            reply2 = chat(
                prompt,
                response_format={"type": "json_object"},
                timeout=180.0,
                max_tokens=6000,
            )
            payload = _extract_json_object(reply2)
        except (json.JSONDecodeError, ValueError) as e2:
            raise EarningsAnalysisError(
                "模型返回内容无法解析为 JSON，请稍后重试。"
            ) from e2
        except RuntimeError as e2:
            raise EarningsAnalysisError(f"调用语言模型失败：{e2}") from e2

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
            "逐字稿来源：本地已解析文本；"
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
