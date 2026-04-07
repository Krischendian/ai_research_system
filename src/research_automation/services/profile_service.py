"""业务画像：基于公开文件节选 + LLM 抽取（禁止臆测与投资建议）。"""
from __future__ import annotations

import json
import re
from datetime import datetime, timezone
from typing import Any

from research_automation.core.database import replace_document_paragraphs
from research_automation.core.paragraph_refs import normalize_paragraph_ref_list
from research_automation.core.paragraph_text import (
    all_paragraph_id_set,
    build_10k_item1_doc_uid,
    index_to_paragraph_id_map,
    make_10k_records,
    paragraphs_to_numbered_excerpt,
    split_into_paragraphs,
)
from research_automation.extractors.llm_client import chat
from research_automation.extractors.sec_edgar import SecEdgarError, get_10k_text, get_cik
from research_automation.models.company import BusinessProfile

# 控制送入 LLM 的 10-K Item 1 长度（全文章节可能极长）
_EXCERPT_CHAR_LIMIT = 120_000


class ProfileGenerationError(Exception):
    """无法生成业务画像（可向 API 客户端返回友好说明）。"""

    def __init__(self, message: str) -> None:
        self.message = message
        super().__init__(message)


def _build_prompt(symbol: str, numbered_paragraphs: str) -> str:
    """numbered_paragraphs：含 PARAGRAPH_ID 行的分段正文（见 paragraph_text）。"""
    return f"""你是一名严谨的披露文件「抽取 extraction」助手：只做从原文逐条提取与轻度压缩整理，禁止「推断 inference」、禁止常识脑补、禁止把模型判断写成公司或管理层观点。

数据源约束（须严格执行）：
1. 一切输出只能来自下方以 PARAGRAPH_ID 标注的段落；不得使用所列段落以外的内容或常识臆测。
2. 文本为 SEC 10-K「Item 1. Business」分段原文（可能截断）；若未包含相关内容，字段须 NOT_FOUND/空数组。
3. 严禁输出投资建议、估值、「应买入/卖出」等。

**段落溯源（强制）**：
- 每个抽取字段必须标注其**主要依据**的段落 ID。
- 可使用简短形式 **p<序号>**（与 PARAGRAPH_ID 行中的 _para_<序号> 一致，例如 p42 对应 …_para_42），或完整写出 PARAGRAPH_ID 字符串；**不得编造**未在下文中出现的 ID。

抽取任务：
- core_business：主营业务中性简述（中文）。
- revenue_by_segment / revenue_by_geography：与原文一致的占比；percentage 须含「%」。
- future_guidance / industry_view：规则同前；无则「原文未明确提及」或 NOT_FOUND。

键 **field_sources**（对象，必须提供）：
- 键名仅限：core_business、future_guidance、industry_view。
- 值为字符串数组，每个元素为 p<正整数> 或完整 PARAGRAPH_ID；**至少列出 1 个**与你填写文案直接相关的段落（若确无可列则为空数组 []）。

revenue_by_segment / revenue_by_geography：每个元素除 segment_name、percentage 外增加 **source_paragraph_ids**（字符串数组，p 形式或完整 ID）。

key_quotes：每元素含 speaker, quote, topic, **source_paragraph_ids**（与 quote 直接相关的段落；须为逐字摘录且出现在对应段落的合并文本中）。

corporate_actions：每元素含 action_type, description, date, source_quote, **source_paragraph_ids**。

输出格式：仅一个 JSON 对象，须含：core_business, revenue_by_segment, revenue_by_geography, future_guidance, industry_view, key_quotes, corporate_actions, field_sources。

用户请求的证券代码（仅供校验）：{symbol}

【分段原文】
{numbered_paragraphs}
"""


def _normalize_verbatim_field(raw: Any) -> str:
    """将 LLM 返回的前景/行业字段规范为字符串；空或仅空白则占位。"""
    if raw is None:
        return "原文未明确提及"
    s = str(raw).strip()
    if not s:
        return "原文未明确提及"
    if s.upper() in ("NOT_FOUND", "NOT FOUND"):
        return "NOT_FOUND"
    return s


def _strip_optional_supporting_quotes(payload: dict[str, Any]) -> None:
    """
    从 payload 中移除仅用于 Prompt 的可选摘录键，避免 Pydantic 校验失败。
    TODO：后续若 BusinessProfile 增加正式溯源字段，可将下列原文在此并入 data_source_label 或独立返回。
    """
    for key in (
        "supporting_quote_future_guidance",
        "supporting_quote_industry_view",
    ):
        payload.pop(key, None)


def _apply_profile_text_fields(payload: dict[str, Any]) -> None:
    """统一填充 future_guidance、industry_view，避免缺键或空串。"""
    payload["future_guidance"] = _normalize_verbatim_field(payload.get("future_guidance"))
    payload["industry_view"] = _normalize_verbatim_field(payload.get("industry_view"))


def _collapse_ws(text: str) -> str:
    """将空白压缩为单空格，便于核对逐字摘录是否在节选内。"""
    return " ".join(text.split())


def _normalize_field_sources(
    payload: dict[str, Any],
    *,
    idx_to_full: dict[int, str],
    allowed: set[str],
) -> None:
    raw = payload.pop("field_sources", None)
    keys = ("core_business", "future_guidance", "industry_view")
    out: dict[str, list[str]] = {k: [] for k in keys}
    if isinstance(raw, dict):
        for k in keys:
            out[k] = normalize_paragraph_ref_list(
                raw.get(k), idx_to_full=idx_to_full, allowed=allowed
            )
    payload["field_paragraph_ids"] = out


def _merge_excerpt_from_paragraphs(records: list[Any]) -> str:
    """用于逐字校验的合并正文（顺序拼接）。"""
    parts: list[str] = []
    for r in records:
        if isinstance(r, dict):
            c = (r.get("content") or "").strip()
        else:
            c = str(r).strip()
        if c:
            parts.append(c)
    return "\n\n".join(parts)


def _collect_profile_cited_ids(payload: dict[str, Any]) -> set[str]:
    cited: set[str] = set()
    fp = payload.get("field_paragraph_ids")
    if isinstance(fp, dict):
        for _k, ids in fp.items():
            if isinstance(ids, list):
                cited.update(str(x) for x in ids if x)
    for key in ("revenue_by_segment", "revenue_by_geography"):
        items = payload.get(key)
        if not isinstance(items, list):
            continue
        for it in items:
            if isinstance(it, dict):
                cited.update(
                    str(x)
                    for x in (it.get("source_paragraph_ids") or [])
                    if x
                )
    for it in payload.get("key_quotes") or []:
        if isinstance(it, dict):
            cited.update(
                str(x) for x in (it.get("source_paragraph_ids") or []) if x
            )
    for it in payload.get("corporate_actions") or []:
        if isinstance(it, dict):
            cited.update(
                str(x) for x in (it.get("source_paragraph_ids") or []) if x
            )
    return cited


def _quote_appears_in_excerpt(quote: str, excerpt: str) -> bool:
    """quote 须在节选内连续出现（允许与节选空白折叠后匹配）。"""
    q = (quote or "").strip()
    if not q:
        return False
    if q in excerpt:
        return True
    return _collapse_ws(q) in _collapse_ws(excerpt)


def _short_topic_label(raw: Any) -> str:
    """topic：英文短标签，约 1–3 词。"""
    s = str(raw or "").strip()
    if not s:
        return "General"
    parts = re.split(r"[\s,/]+", s)
    parts = [p for p in parts if p]
    if len(parts) <= 3:
        return " ".join(parts)
    return " ".join(parts[:3])


def _normalize_key_quotes(
    payload: dict[str, Any],
    excerpt: str,
    *,
    idx_to_full: dict[int, str],
    allowed: set[str],
) -> None:
    """
    规范化 key_quotes：校验结构、发言人默认值、topic 长度，并丢弃无法在节选内匹配的 quote（防止非逐字内容）。
    """
    raw = payload.get("key_quotes")
    if not isinstance(raw, list):
        payload["key_quotes"] = []
        return

    cleaned: list[dict[str, Any]] = []
    for it in raw:
        if not isinstance(it, dict):
            continue
        quote = str(it.get("quote", "")).strip()
        if not quote or not _quote_appears_in_excerpt(quote, excerpt):
            continue
        sp = str(it.get("speaker", "")).strip()
        if not sp:
            sp = "UNKNOWN"
        sp_ids = normalize_paragraph_ref_list(
            it.get("source_paragraph_ids"),
            idx_to_full=idx_to_full,
            allowed=allowed,
        )
        cleaned.append(
            {
                "speaker": sp,
                "quote": quote,
                "topic": _short_topic_label(it.get("topic")),
                "source_paragraph_ids": sp_ids,
            }
        )
        if len(cleaned) >= 5:
            break

    payload["key_quotes"] = cleaned


_CORPORATE_ACTION_TYPES = frozenset({"new_business", "acquisition", "partnership"})


def _normalize_action_type(raw: Any) -> str | None:
    """将 LLM 返回的类型规范为三种枚举之一；无法识别则返回 None（整条丢弃）。"""
    s = str(raw or "").strip().lower().replace("-", "_").replace(" ", "_")
    if s in _CORPORATE_ACTION_TYPES:
        return s
    if s in ("merge", "merger", "m_a", "mna"):
        return "acquisition"
    if s in ("partner", "strategic_partnership", "alliance"):
        return "partnership"
    if s in ("newbusiness", "new_product", "product_launch"):
        return "new_business"
    return None


def _normalize_optional_date(raw: Any) -> str | None:
    if raw is None:
        return None
    s = str(raw).strip()
    return s if s else None


def _normalize_corporate_actions(
    payload: dict[str, Any],
    excerpt: str,
    *,
    idx_to_full: dict[int, str],
    allowed: set[str],
) -> None:
    """校验 dynamic 条目：类型枚举、source_quote 须在节选内逐字出现，description 非空。"""
    raw = payload.get("corporate_actions")
    if not isinstance(raw, list):
        payload["corporate_actions"] = []
        return

    cleaned: list[dict[str, Any]] = []
    for it in raw:
        if not isinstance(it, dict):
            continue
        at = _normalize_action_type(it.get("action_type"))
        if at is None:
            continue
        quote = str(it.get("source_quote", "")).strip()
        if not quote or not _quote_appears_in_excerpt(quote, excerpt):
            continue
        desc = str(it.get("description", "")).strip()
        if not desc:
            continue
        sp_ids = normalize_paragraph_ref_list(
            it.get("source_paragraph_ids"),
            idx_to_full=idx_to_full,
            allowed=allowed,
        )
        cleaned.append(
            {
                "action_type": at,
                "description": desc,
                "date": _normalize_optional_date(it.get("date")),
                "source_quote": quote,
                "source_paragraph_ids": sp_ids,
            }
        )

    payload["corporate_actions"] = cleaned


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


def _normalize_percentage(value: str) -> str:
    """确保占比字符串含「%」（若模型漏写则补全）。"""
    s = (value or "").strip()
    if not s:
        return "0%"
    if "%" not in s:
        return f"{s}%"
    return s


def _normalize_mix_lists(
    data: dict[str, Any],
    *,
    idx_to_full: dict[int, str],
    allowed: set[str],
) -> None:
    for key in ("revenue_by_segment", "revenue_by_geography"):
        items = data.get(key)
        if not isinstance(items, list):
            data[key] = []
            continue
        fixed: list[dict[str, Any]] = []
        for it in items:
            if not isinstance(it, dict):
                continue
            name = it.get("segment_name")
            pct = it.get("percentage")
            if name is None or pct is None:
                continue
            sp_ids = normalize_paragraph_ref_list(
                it.get("source_paragraph_ids"),
                idx_to_full=idx_to_full,
                allowed=allowed,
            )
            fixed.append(
                {
                    "segment_name": str(name).strip(),
                    "percentage": _normalize_percentage(str(pct)),
                    "source_paragraph_ids": sp_ids,
                }
            )
        data[key] = fixed


def get_profile(ticker: str) -> BusinessProfile:
    """
    从 SEC EDGAR 拉取最近申报的 10-K「Item 1. Business」全文（经缓存），再调用 LLM 生成 ``BusinessProfile``。
    失败时抛出 ``ProfileGenerationError``（带可读中文说明）。
    """
    symbol = (ticker or "").strip().upper() or "UNKNOWN"
    filing_year = datetime.now(timezone.utc).year - 1
    try:
        excerpt_full = get_10k_text(symbol, filing_year)
    except SecEdgarError as e:
        raise ProfileGenerationError(f"无法获取 SEC 10-K 文本：{e}") from e

    excerpt = excerpt_full[:_EXCERPT_CHAR_LIMIT]
    doc_uid = build_10k_item1_doc_uid(symbol, filing_year)
    chunks = split_into_paragraphs(excerpt)
    records = make_10k_records(doc_uid, chunks)
    replace_document_paragraphs(
        doc_uid,
        symbol,
        "10K_ITEM1",
        context_year=filing_year,
        quarter_label=None,
        records=[
            (r["paragraph_id"], r["para_index"], r["content"]) for r in records
        ],
    )
    numbered = paragraphs_to_numbered_excerpt(records)
    idx_to_full = index_to_paragraph_id_map(records)
    allowed = all_paragraph_id_set(records)
    id_to_text = {r["paragraph_id"]: r["content"] for r in records}
    merged_excerpt = _merge_excerpt_from_paragraphs(records)

    prompt = _build_prompt(symbol, numbered)
    try:
        reply = chat(
            prompt,
            response_format={"type": "json_object"},
            timeout=90.0,
        )
    except ValueError as e:
        raise ProfileGenerationError(f"语言模型未就绪：{e}") from e
    except RuntimeError as e:
        raise ProfileGenerationError(f"调用语言模型失败：{e}") from e

    try:
        payload = _extract_json_object(reply)
    except (json.JSONDecodeError, ValueError) as e:
        raise ProfileGenerationError(
            "模型返回内容无法解析为 JSON，请稍后重试或检查服务日志。"
        ) from e

    payload["ticker"] = symbol
    payload["last_updated"] = datetime.now(timezone.utc).isoformat()
    payload["data_source_label"] = (
        f"SEC EDGAR 10-K「Item 1. Business」原文（标的 {symbol}，优先匹配 "
        f"{filing_year} 公历年申报；可能截断前 {_EXCERPT_CHAR_LIMIT} 字符）+ OpenAI 结构化抽取。"
    )
    try:
        cik = get_cik(symbol)
        payload["primary_source_url"] = (
            f"https://www.sec.gov/cgi-bin/browse-edgar?action=getcompany&CIK={cik}&type=10-K&owner=exclude&count=40"
        )
    except SecEdgarError:
        payload["primary_source_url"] = f"https://www.sec.gov/edgar/search/#/q={symbol}"
    _normalize_mix_lists(payload, idx_to_full=idx_to_full, allowed=allowed)
    _strip_optional_supporting_quotes(payload)
    _apply_profile_text_fields(payload)
    _normalize_field_sources(payload, idx_to_full=idx_to_full, allowed=allowed)
    _normalize_key_quotes(
        payload,
        merged_excerpt,
        idx_to_full=idx_to_full,
        allowed=allowed,
    )
    _normalize_corporate_actions(
        payload,
        merged_excerpt,
        idx_to_full=idx_to_full,
        allowed=allowed,
    )
    payload["document_uid"] = doc_uid
    cited = _collect_profile_cited_ids(payload)
    payload["source_paragraphs"] = {
        pid: id_to_text[pid] for pid in cited if pid in id_to_text
    }

    try:
        return BusinessProfile.model_validate(payload)
    except Exception as e:
        raise ProfileGenerationError(
            f"业务画像字段未通过校验（占比须含 % 等）：{e}"
        ) from e
