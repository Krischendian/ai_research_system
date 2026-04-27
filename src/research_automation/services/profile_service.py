"""业务画像：基于公开文件节选 + LLM 抽取（禁止臆测与投资建议）。"""
from __future__ import annotations

import json
import logging
import os
import re
from datetime import date, datetime, timedelta, timezone
from typing import Any

from research_automation.core.database import (
    get_step_cache,
    replace_document_paragraphs,
    set_step_cache,
)
from research_automation.core.paragraph_refs import normalize_paragraph_ref_list
from research_automation.core.paragraph_text import (
    all_paragraph_id_set,
    build_10k_profile_doc_uid,
    index_to_paragraph_id_map,
    make_10k_records,
    paragraphs_to_numbered_excerpt,
    split_into_paragraphs,
)
from research_automation.core.verbatim_match import quote_matches_haystack
from research_automation.extractors.llm_client import chat
from research_automation.core.ticker_normalize import normalize_equity_ticker
from research_automation.extractors.fmp_client import get_segment_revenue
from research_automation.extractors.sec_edgar import (
    SecEdgarError,
    get_10k_sections,
    get_cik,
)
from research_automation.extractors.news_client import RawArticle
from research_automation.models.company import BusinessProfile
from research_automation.services.earnings_service import (
    EarningsAnalysisError,
    analyze_earnings_call,
)
from research_automation.services.news_service import (
    get_company_news,
    raw_article_to_profile_news_fields,
)

from research_automation.services.hallucination_guard import check_profile_fields

logger = logging.getLogger(__name__)

# 合并后总字符上限（约 14–16k tokens 英文量级）；单章裁剪之和应 ≤ 此值，避免 ``merged[:N]`` 砍掉 Item 8
_PROFILE_MERGED_MAX_CHARS = 58_000
# 单章上限。Apple 等常在 Item 1 末段用文字列出分部/地区占比，item1 须留足字符以免 LLM 看不到。
_PROFILE_SECTION_CHAR_LIMITS: dict[str, int] = {
    "item1": 18_000,
    "item7": 14_000,
    "item8_notes": 20_000,
    "item1a": 5_000,
}

_FMP_SEGMENT_PCT_WARN_THRESHOLD = 10.0
_FMP_SEGMENT_VALIDATION_WARNING = "业务线占比与财报披露偏差较大，请人工复核"
_PROFILE_CACHE_VERSION = 1

_PROFILE_SECTION_HEADERS: tuple[tuple[str, str], ...] = (
    ("ITEM_1_BUSINESS", "item1"),
    ("ITEM_7_MD_AND_A", "item7"),
    ("ITEM_8_NOTES_SEGMENTS", "item8_notes"),
    ("ITEM_1A_RISK_FACTORS", "item1a"),
)


class ProfileGenerationError(Exception):
    """无法生成业务画像（可向 API 客户端返回友好说明）。"""

    def __init__(self, message: str) -> None:
        self.message = message
        super().__init__(message)


def _trim_section_text(key: str, body: str) -> str:
    lim = _PROFILE_SECTION_CHAR_LIMITS.get(key)
    if not lim or len(body) <= lim:
        return body
    return body[:lim] + "\n\n[... SECTION TRUNCATED ...]"


def _merge_sections_for_profile(sections: dict[str, str]) -> str:
    """按固定顺序合并各章节；先按单章上限裁剪，再按总上限截断。"""
    parts: list[str] = []
    for label, key in _PROFILE_SECTION_HEADERS:
        raw = (sections.get(key) or "").strip()
        if not raw:
            continue
        body = _trim_section_text(key, raw)
        parts.append(f"=== {label} ===\n\n{body}")
    merged = "\n\n".join(parts)
    if len(merged) > _PROFILE_MERGED_MAX_CHARS:
        merged = merged[:_PROFILE_MERGED_MAX_CHARS] + "\n\n[... TRUNCATED ...]"
    return merged


def _build_prompt(symbol: str, numbered_paragraphs: str) -> str:
    """numbered_paragraphs：含 PARAGRAPH_ID 行的分段正文（见 paragraph_text）。"""
    return f"""你是一名严谨的披露文件「抽取 extraction」助手：只做从原文逐条提取与轻度压缩整理，禁止「推断 inference」、禁止常识脑补、禁止把模型判断写成公司或管理层观点。

━━━ ANTI-HALLUCINATION RULES（最高优先级，违反不可接受）━━━
RULE 1 - SOURCE ONLY:
  只能输出下方原文中明确出现的信息。不得用行业背景知识补充，不得推断，不得脑补。
  若某字段在原文中无依据，必须输出 null，禁止猜测填充。

RULE 2 - PRESERVE MODALITY（情态词必须原样保留）:
  原文中的情态动词和时态必须在输出中完整保留，禁止升级为确定性陈述。
  ❌ 错误示例："该公司正在投资 X 方向" / "已开始扩张"
  ✅ 正确示例："该公司预计将投资 X 方向" / "计划于明年扩张"
  涉及词汇：expects / plans / anticipates / intends / may / considers / targets / aims to
  以上词出现时，中文输出必须对应使用：预计/计划/拟/考虑/可能/旨在，不得用"已""正在""开始"替代。

RULE 3 - VERBATIM QUOTES ONLY:
  key_quotes 中的 quote 字段必须与原文逐字一致（verbatim substring）。
  禁止转述、意译、缩写。若无法找到逐字原文，该条 key_quotes 不得输出。

RULE 4 - ONE COMPANY SCOPE:
  本 prompt 仅处理 {symbol} 这一家公司。
  禁止引用或推断其他公司的信息，即使原文中有对比内容。
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

数据源约束（须严格执行）：
1. 一切输出只能来自下方以 PARAGRAPH_ID 标注的段落；不得使用所列段落以外的内容或常识臆测。
2. 正文由 SEC 10-K 多个章节合并并可能截断；**出现顺序**为：ITEM_1_BUSINESS → ITEM_7_MD_AND_A → ITEM_8_NOTES_SEGMENTS → ITEM_1A_RISK_FACTORS（分部营收多在 Item 8 附注窗口内）。
3. 严禁输出投资建议、估值、「应买入/卖出」等。

**字段与章节对应（须遵守）**：
- **core_business**：仅依据 **ITEM_1_BUSINESS** 章节，用中文中性概述主营业务与产品/服务形态。
- **future_guidance**：仅依据 **ITEM_7_MD_AND_A** 中管理层对未来业绩、指引、趋势等**明确表述**；若该章节未出现此类内容，JSON 中填 **null**（不要写「未提及」类文字）。【严格遵守 RULE 2：原文若用 expects/plans/may，中文输出必须用「预计/计划/可能」，禁止写成已完成或现在进行时】。
- **industry_view_text**：仅依据 **ITEM_7_MD_AND_A** 中对行业、竞争、宏观环境的**明确表述**，用**中文**压缩归纳（**不超过 80 个汉字**）；若无可用依据则 **null**（勿写「未提及」类占位句）。
- **industry_view_source**：与 **industry_view_text** 对应的**原文依据**字符串，供用户核对。须二选一形式：（1）本文件分段 ID + 英文逐字摘录，如 ``10K_PROFILE_xxx:p12: 'The markets for the Company's products...'`` 或 ``p12: '...'``（引号内与原文**完全一致**的连续子串）；（2）或 ``Item 7 MD&A: '...'`` 仅英文逐字摘录（≥20 字符为宜）。**禁止**臆造未出现的句子。若 **industry_view_text** 为 **null**，则 **industry_view_source** 也必须为 **null**。
- **key_quotes**：优先 **ITEM_7_MD_AND_A**；可含 **ITEM_1_BUSINESS** 中带引号或「某某 stated」类**可归属**管理层/董事的原文。**禁止**从 **ITEM_1A_RISK_FACTORS** 抽取（风险因素模板句不算管理层针对性表态）。每元素：speaker（原文能识别则写姓名或职务，否则 "UNKNOWN"）、quote（与原文**逐字一致**的英文连续片段，遵守 RULE 3，不得转述）、topic（英文短标签 1–3 词）、modality（必填，三选一："fact" = 已发生事实 / "forward_looking" = 含 expects/plans/may 等前瞻表述 / "uncertain" = 模糊表述）。
- **corporate_actions**：从 **ITEM_7_MD_AND_A** 与 **ITEM_1A_RISK_FACTORS** 中抽取新业务、并购、重大合作等；每元素须含可在原文中逐字找到的 source_quote。

**营收拆分（极其重要）**：
- **revenue_by_segment**：仅填**产品/服务业务线**（如 iPhone、Mac、Services、Azure、Advertising），数据须来自 **ITEM_8_NOTES_SEGMENTS** 或 **ITEM_1_BUSINESS** 中明确列示的「按产品线/业务分部」占比。**禁止**把分销渠道、销售方式（如 Indirect Distribution、Retail）或地理名称填入本列表。
- **revenue_by_geography**：仅填**国家/区域**（如 Americas、Europe、Greater China、United States），优先 **ITEM_8_NOTES_SEGMENTS** 中的地理分部披露；若无则 **ITEM_1_BUSINESS** 中明确地理占比。Americas / Europe 等属于本列表，**不得**放入 revenue_by_segment。
- 若 **ITEM_1_BUSINESS** 中出现 ``net sales by reportable segment``、``net sales by region`` 或「iPhone / Mac / Americas + 百分号」等列举，**必须**填入对应 revenue 列表（不得留空）；可与 Item 8 附注数据合并理解，但须来自原文数字。
- 若某类占比在全文均不存在，对应列表为 **[]**。percentage 字符串须含「%」。

**段落溯源（强制）**：
- 键 **field_sources**：键名仅限 core_business、future_guidance、industry_view；值为 p<序号> 或完整 PARAGRAPH_ID 的数组（无依据则 []）。**industry_view** 的数组须覆盖 **industry_view_source** 所依据的段落（摘录出自 p12 则须含 p12 或对应 PARAGRAPH_ID）。
- revenue_by_segment、revenue_by_geography、key_quotes、corporate_actions 各条须含 **source_paragraph_ids**。

输出格式：仅一个 JSON 对象，须含：core_business, revenue_by_segment, revenue_by_geography, future_guidance, industry_view_text, industry_view_source, key_quotes, corporate_actions, field_sources。其中 future_guidance、industry_view_text、industry_view_source 均可为 null；无行业判断依据时 **industry_view_text** 与 **industry_view_source** 须同时为 null。

用户请求的证券代码（仅供校验）：{symbol}

【分段原文】
{numbered_paragraphs}
"""


def _normalize_verbatim_field(raw: Any) -> str:
    """将 LLM 返回的前景/行业字段规范为字符串；空或仅空白则占位。"""
    if raw is None:
        return "原文未明确提及"

    # 如果是列表（LLM返回结构化数组），提取item字段拼成文字
    if isinstance(raw, list):
        items = []
        for entry in raw:
            if isinstance(entry, dict):
                text = entry.get("item") or entry.get("text") or entry.get("content") or ""
                if text:
                    items.append(str(text).strip())
            elif isinstance(entry, str) and entry.strip():
                items.append(entry.strip())
        if items:
            return "；".join(items)
        return "原文未明确提及"

    # 如果是字符串但内容是列表格式，尝试解析
    s = str(raw).strip()
    if not s:
        return "原文未明确提及"
    if s.lower() in ("null", "none"):
        return "原文未明确提及"
    if s.upper() in ("NOT_FOUND", "NOT FOUND"):
        return "NOT_FOUND"
    if s.startswith("[") and s.endswith("]"):
        try:
            import ast

            parsed = ast.literal_eval(s)
            if isinstance(parsed, list):
                return _normalize_verbatim_field(parsed)
        except Exception:
            pass
    return s


def _normalize_industry_view_source(raw: Any) -> str | None:
    """行业判断原文摘录；无则 None。"""
    if raw is None:
        return None
    s = str(raw).strip()
    if not s or s.lower() in ("null", "none"):
        return None
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
    """统一填充 future_guidance、industry_view、industry_view_source。"""
    payload["future_guidance"] = _normalize_verbatim_field(payload.get("future_guidance"))

    iv_new = payload.pop("industry_view_text", None)
    iv_legacy = payload.pop("industry_view", None)
    if iv_new is not None:
        payload["industry_view"] = _normalize_verbatim_field(iv_new)
    else:
        payload["industry_view"] = _normalize_verbatim_field(iv_legacy)

    iv_src = _normalize_industry_view_source(payload.pop("industry_view_source", None))
    if payload["industry_view"] == "原文未明确提及":
        payload["industry_view_source"] = None
    else:
        payload["industry_view_source"] = iv_src


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
    """quote 须在节选内出现：先逐字归一化子串，再模糊匹配（空白/标点/大小写）。"""
    return quote_matches_haystack(quote, excerpt, similarity_threshold=0.9)


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
        if len(cleaned) >= 8:
            break

    payload["key_quotes"] = cleaned


def _earnings_deep_link_url(ticker: str, quarter_label: str) -> str:
    origin = (
        os.environ.get("RESEARCH_STREAMLIT_ORIGIN") or "http://127.0.0.1:8501"
    ).rstrip("/")
    sym = (ticker or "").strip().upper()
    q = (quarter_label or "").strip()
    return f"{origin}/DeepDive?ticker={sym}&quarter={q}"


def _merge_earnings_quotations_into_profile(
    payload: dict[str, Any],
    symbol: str,
    filing_year: int,
) -> dict[str, str]:
    """
    将 ``analyze_earnings_call`` 返回的 ``quotations`` 并入画像 ``key_quotes``，
    并返回电话会 ``source_paragraphs`` 映射供写入响应（不丢弃段落原文）。
    """
    try:
        analysis = analyze_earnings_call(symbol, filing_year, 4)
    except EarningsAnalysisError as e:
        logger.info(
            "业务画像：未合并电话会原话 ticker=%s year=%sQ4：%s",
            symbol,
            filing_year,
            e.message,
        )
        return {}
    except Exception:
        logger.exception(
            "业务画像：拉取电话会分析异常 ticker=%s %sQ4，跳过合并",
            symbol,
            filing_year,
        )
        return {}

    sp_map: dict[str, str] = {}
    raw_sp = getattr(analysis, "source_paragraphs", None) or {}
    if isinstance(raw_sp, dict):
        for k, v in raw_sp.items():
            if k and v is not None:
                sp_map[str(k)] = str(v)

    kq_list = payload.get("key_quotes")
    if not isinstance(kq_list, list):
        kq_list = []
        payload["key_quotes"] = kq_list

    qlabel = str(analysis.quarter or f"{filing_year}Q4").strip()
    deep_url = _earnings_deep_link_url(symbol, qlabel)

    for eq in analysis.quotations or []:
        sp_ids = [str(x) for x in (eq.source_paragraph_ids or []) if x]
        kq_list.append(
            {
                "speaker": (eq.speaker or "").strip() or "UNKNOWN",
                "quote": str(eq.quote or "").strip(),
                "topic": _short_topic_label(eq.topic),
                "source_paragraph_ids": sp_ids,
                "data_source": "earnings_call",
                "source_url": deep_url,
            }
        )

    if analysis.quotations:
        note = (
            f" 已合并 {symbol} 财报电话会（{qlabel}）"
            f"「quotations」{len(analysis.quotations)} 条原话，段落原文见 source_paragraphs；"
            "各条 ``data_source=earnings_call``，``source_url`` 指向深度分析页。"
        )
        payload["data_source_label"] = (payload.get("data_source_label") or "").rstrip() + note

    return sp_map


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
        row: dict[str, Any] = {
            "action_type": at,
            "description": desc,
            "date": _normalize_optional_date(it.get("date")),
            "source_quote": quote,
            "source_paragraph_ids": sp_ids,
        }
        su = str(it.get("source_url") or "").strip()
        if su:
            row["source_url"] = su
        cleaned.append(row)

    payload["corporate_actions"] = cleaned


def _news_action_time_key(article: RawArticle) -> float:
    if "finnhub_datetime_unix" in article:
        try:
            return float(article["finnhub_datetime_unix"])
        except (TypeError, ValueError):
            pass
    pa = (article.get("published_at_utc") or "").strip()
    if pa:
        try:
            iso = pa.replace("Z", "+00:00")
            return datetime.fromisoformat(iso).timestamp()
        except (ValueError, TypeError, OSError):
            pass
    return 0.0


def _classify_news_corporate_action_type(blob: str) -> str | None:
    """根据标题+摘要关键词归类为三种动态类型；无匹配则跳过该新闻。"""
    b = (blob or "").lower()
    acq_kw = (
        "merger",
        "acquisitions",
        "acquisition",
        "acquire",
        "acquires",
        "acquired",
        "buyout",
        "takeover",
        "m&a",
        "merges with",
        "to acquire",
        "agrees to buy",
    )
    if any(k in b for k in acq_kw):
        return "acquisition"
    if any(k in blob for k in ("收购", "并购")):
        return "acquisition"
    partner_kw = (
        "partnership",
        "strategic partnership",
        "strategic alliance",
        "collaboration",
        "teams up",
        "partners with",
        "teams with",
    )
    if any(k in b for k in partner_kw):
        return "partnership"
    if any(k in blob for k in ("战略合作", "合作伙伴", "达成合作协议")):
        return "partnership"
    nb_kw = (
        "launch",
        "launches",
        "unveil",
        "unveils",
        "introduces",
        "expansion",
        "expands",
        "new product",
        "opens ",
        "debut",
        " rollout",
        "roll-out",
    )
    if any(k in b for k in nb_kw):
        return "new_business"
    if any(k in blob for k in ("推出", "发布", "新产品", "扩张")):
        return "new_business"
    return None


def _append_news_corporate_actions_to_profile(payload: dict[str, Any], symbol: str) -> None:
    """
    自 ``get_company_news`` 补充 ``CorporateAction``：须含 ``source_url``，无链接则跳过。
    """
    sym = (symbol or "").strip().upper()
    if not sym:
        return
    to_d = date.today()
    from_d = to_d - timedelta(days=120)
    try:
        articles = get_company_news(sym, from_d, to_d)
    except Exception:
        logger.exception("业务画像：拉取公司新闻失败 ticker=%s，跳过新闻动态", sym)
        return
    if not articles:
        return

    ranked = sorted(articles, key=_news_action_time_key, reverse=True)
    seen_urls: set[str] = set()
    existing = payload.get("corporate_actions")
    if not isinstance(existing, list):
        existing = []
        payload["corporate_actions"] = existing

    added = 0
    max_news = 8
    for art in ranked[:40]:
        fields = raw_article_to_profile_news_fields(art)
        if fields is None:
            continue
        url = fields["source_url"]
        if url in seen_urls:
            continue
        title = fields["title"]
        summary = fields["summary"]
        blob = f"{title}\n{summary}"
        at = _classify_news_corporate_action_type(blob)
        if at is None:
            continue
        seen_urls.add(url)
        desc = title if title else summary
        if not desc:
            continue
        quote = summary if summary else title
        if not quote:
            continue
        pub_raw = fields["published_at"].strip()
        pub = pub_raw if pub_raw else None
        existing.append(
            {
                "action_type": at,
                "description": desc,
                "date": pub,
                "source_quote": quote,
                "source_paragraph_ids": [],
                "source_url": url,
            }
        )
        added += 1
        if added >= max_news:
            break

    if added:
        note = (
            f" 已从公司新闻（Benzinga/Finnhub 等）补充 {added} 条近期动态；"
            "条目均含 ``source_url`` 原文链接，无链接的新闻不会写入。"
        )
        payload["data_source_label"] = (payload.get("data_source_label") or "").rstrip() + note


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


# Apple 等常见：Item 1 散文列举「iPhone 52% of total net sales; Mac 10%; …」
_PRODUCT_SEG_RE = re.compile(
    r"(?is)\b(iPhone|Mac|iPad|Services|Wearables,?\s+Home\s+and\s+Accessories)\s+(\d+)\s*%"
)
_GEO_PCT_RE = re.compile(
    r"(?is)\b(Americas|Europe|Greater\s+China|Japan|Rest\s+of\s+Asia\s+Pacific)\s+(\d+)\s*%"
)


def _parse_product_line_percents(item1: str) -> list[tuple[str, str]]:
    seen: set[str] = set()
    out: list[tuple[str, str]] = []
    for m in _PRODUCT_SEG_RE.finditer(item1 or ""):
        name = re.sub(r"\s+", " ", m.group(1).strip())
        pct = m.group(2)
        k = name.lower()
        if k not in seen:
            seen.add(k)
            out.append((name, pct))
    return out


def _parse_geography_percents(item1: str) -> list[tuple[str, str]]:
    seen: set[str] = set()
    out: list[tuple[str, str]] = []
    for m in _GEO_PCT_RE.finditer(item1 or ""):
        name = re.sub(r"\s+", " ", m.group(1).strip())
        pct = m.group(2)
        k = name.lower()
        if k not in seen:
            seen.add(k)
            out.append((name, pct))
    return out


def _mix_source_paragraph_ids(
    records: list[Any],
    label: str,
    pct: str,
) -> list[str]:
    needle = f"{pct}%"
    lab_l = (label or "").lower()
    for r in records:
        c = r.get("content") or ""
        if needle not in c and f"{pct} %" not in c:
            continue
        if lab_l in c.lower():
            return [str(r["paragraph_id"])]
    token = (label.split() or [""])[0].lower()
    if len(token) >= 3:
        for r in records:
            c = r.get("content") or ""
            if needle in c and token in c.lower():
                return [str(r["paragraph_id"])]
    return []


def _apply_item1_revenue_mix_fallback(
    payload: dict[str, Any],
    sections: dict[str, str],
    records: list[Any],
) -> bool:
    """
    当模型未返回分部/地区列表时，从 **完整** Item 1 原文（非合并裁剪版）用规则解析补全。
    """
    item1 = sections.get("item1") or ""
    if not item1.strip():
        return False
    changed = False
    rs = payload.get("revenue_by_segment")
    if not isinstance(rs, list) or len(rs) == 0:
        pairs = _parse_product_line_percents(item1)
        if pairs:
            payload["revenue_by_segment"] = [
                {
                    "segment_name": n,
                    "percentage": _normalize_percentage(p),
                    "source_paragraph_ids": _mix_source_paragraph_ids(records, n, p),
                }
                for n, p in pairs
            ]
            changed = True
    rg = payload.get("revenue_by_geography")
    if not isinstance(rg, list) or len(rg) == 0:
        gpairs = _parse_geography_percents(item1)
        if gpairs:
            payload["revenue_by_geography"] = [
                {
                    "segment_name": n,
                    "percentage": _normalize_percentage(p),
                    "source_paragraph_ids": _mix_source_paragraph_ids(records, n, p),
                }
                for n, p in gpairs
            ]
            changed = True
    return changed


# 新版 10-K（如 Apple）在 Item 8 用「百万美元」表披露，而非 Item 1 散文百分比
_ITEM8_CAT_HEAD = "net sales by category for"
_ITEM8_GEO_HEAD = "net sales by reportable segment for"
_ITEM8_CAT_ROW = re.compile(
    r"(?is)\b(iPhone|Mac|iPad|Wearables,?\s+Home\s+and\s+Accessories|Services)"
    r"(?:\s*\(1\))?\s*\$?\s*([\d,]+)"
)
_ITEM8_GEO_ROW = re.compile(
    r"(?is)\b(Americas|Europe|Greater\s+China|Japan|Rest\s+of\s+Asia\s+Pacific)\s*\$?\s*([\d,]+)"
)


def _slice_until_first_total_net_sales(blob: str, header_lc: str) -> str:
    low = blob.lower()
    i = low.find(header_lc)
    if i < 0:
        return ""
    w = blob[i : i + 16_000]
    m = re.search(r"(?is)Total\s+net\s+sales\s*\$?\s*[\d,]+", w)
    if not m:
        return w
    return w[: m.end()]


def _total_net_sales_millions(window: str) -> float | None:
    m = re.search(r"(?is)Total\s+net\s+sales\s*\$?\s*([\d,]+)", window)
    if not m:
        return None
    v = float(m.group(1).replace(",", ""))
    return v if v > 0 else None


def _named_amount_rows(
    window: str,
    row_re: re.Pattern[str],
    *,
    min_millions: float,
) -> list[tuple[str, float]]:
    seen: set[str] = set()
    out: list[tuple[str, float]] = []
    for m in row_re.finditer(window):
        name = re.sub(r"\s+", " ", m.group(1).strip())
        amt = float(m.group(2).replace(",", ""))
        if amt < min_millions:
            continue
        k = name.lower()
        if k in seen:
            continue
        seen.add(k)
        out.append((name, amt))
    return out


def _percent_mix_from_dollar_table(
    blob: str,
    header_lc: str,
    row_re: re.Pattern[str],
) -> list[tuple[str, str]]:
    win = _slice_until_first_total_net_sales(blob, header_lc)
    if not win.strip():
        return []
    total = _total_net_sales_millions(win)
    if not total:
        return []
    rows = _named_amount_rows(win, row_re, min_millions=5_000.0)
    if len(rows) < 2:
        return []
    summed = sum(a for _, a in rows)
    if summed <= 0 or abs(summed - total) / total > 0.02:
        return []
    return [(n, f"{(a / total * 100):.1f}%") for n, a in rows]


def _paragraph_ids_with_marker(records: list[Any], marker: str) -> list[str]:
    ml = marker.lower()
    for r in records:
        c = (r.get("content") or "")
        if ml in c.lower():
            return [str(r["paragraph_id"])]
    return []


def _apply_item8_dollar_table_fallback(
    payload: dict[str, Any],
    sections: dict[str, str],
    records: list[Any],
) -> bool:
    """
    从 Item 7 + Item 8 附注纯文本中的「Net sales by category / reportable segment」表
    用**首列**财年金额与 Total 推算占比（适配 Apple 等发行人）。
    """
    blob = f"{sections.get('item8_notes') or ''}\n{sections.get('item7') or ''}"
    if not blob.strip():
        return False
    changed = False
    rs = payload.get("revenue_by_segment")
    if not isinstance(rs, list) or len(rs) == 0:
        pairs = _percent_mix_from_dollar_table(
            blob, _ITEM8_CAT_HEAD, _ITEM8_CAT_ROW
        )
        if pairs:
            pids = _paragraph_ids_with_marker(records, "net sales by category")
            payload["revenue_by_segment"] = [
                {
                    "segment_name": n,
                    "percentage": _normalize_percentage(p),
                    "source_paragraph_ids": pids,
                }
                for n, p in pairs
            ]
            changed = True
    rg = payload.get("revenue_by_geography")
    if not isinstance(rg, list) or len(rg) == 0:
        gpairs = _percent_mix_from_dollar_table(
            blob, _ITEM8_GEO_HEAD, _ITEM8_GEO_ROW
        )
        if gpairs:
            pids = _paragraph_ids_with_marker(
                records, "net sales by reportable segment"
            )
            payload["revenue_by_geography"] = [
                {
                    "segment_name": n,
                    "percentage": _normalize_percentage(p),
                    "source_paragraph_ids": pids,
                }
                for n, p in gpairs
            ]
            changed = True
    return changed


def _parse_mix_percentage_float(raw: str) -> float | None:
    m = re.search(r"([\d.]+)\s*%", str(raw or ""))
    if not m:
        return None
    try:
        return float(m.group(1))
    except ValueError:
        return None


def _norm_segment_compact(name: str) -> str:
    return re.sub(r"[^a-z0-9]+", "", (name or "").lower())


def _stem_token(w: str) -> str:
    if len(w) > 4 and w.endswith("s") and not w.endswith("ss"):
        return w[:-1]
    return w


def _fmp_segment_matches_llm(fmp_name: str, llm_name: str) -> bool:
    """英文分部名与 LLM 抽取名宽松对齐（含 Service/Services、子串等）。"""
    a, b = _norm_segment_compact(fmp_name), _norm_segment_compact(llm_name)
    if not a or not b:
        return False
    if a == b:
        return True
    if a in b or b in a:
        return min(len(a), len(b)) >= 4
    wa = set(re.findall(r"[a-z]{3,}", fmp_name.lower()))
    wb = set(re.findall(r"[a-z]{3,}", llm_name.lower()))
    wa2 = {_stem_token(x) for x in wa} | wa
    wb2 = {_stem_token(x) for x in wb} | wb
    if wa & wb:
        return True
    if wa2 & wb2:
        return True
    return False


def _best_llm_match_for_fmp_segment(
    fmp_name: str,
    parsed_llm: list[tuple[str, float]],
) -> tuple[str, float] | None:
    hits = [
        (ln, lp)
        for ln, lp in parsed_llm
        if _fmp_segment_matches_llm(fmp_name, ln)
    ]
    if not hits:
        return None
    return max(hits, key=lambda x: len(_norm_segment_compact(x[0])))


def _fmp_segment_rows_to_mix_payload(
    rows: list[dict[str, Any]],
) -> list[dict[str, Any]]:
    return [
        {
            "segment_name": str(r["segment"]),
            "percentage": f"{float(r['percentage']):.1f}%",
            "source_paragraph_ids": [],
        }
        for r in rows
    ]


def _apply_fmp_segment_validation(
    payload: dict[str, Any],
    symbol: str,
    filing_year: int,
    *,
    idx_to_full: dict[int, str],
    allowed: set[str],
) -> None:
    """
    以 FMP 分部营收为基准：列表为空或占比不可解析时用 FMP 覆盖 ``revenue_by_segment``；
    否则对能对齐的分部比较占比，绝对差超过阈值则写入 ``validation_warning``。
    """
    rows = get_segment_revenue(symbol, filing_year)
    if not rows:
        return

    segs = payload.get("revenue_by_segment")
    if not isinstance(segs, list):
        segs = []

    if len(segs) == 0:
        payload["revenue_by_segment"] = _fmp_segment_rows_to_mix_payload(rows)
        payload["data_source_label"] = (
            (payload.get("data_source_label") or "").rstrip()
            + " 业务线营收占比未从披露节选取得时，已使用 FMP Revenue Product Segmentation（"
            f"{filing_year} 财年口径）按分部金额占合计比例填充；无 SEC 段落链接。"
        )
        _normalize_mix_lists(payload, idx_to_full=idx_to_full, allowed=allowed)
        return

    parsed_llm: list[tuple[str, float]] = []
    for it in segs:
        if not isinstance(it, dict):
            continue
        nm = str(it.get("segment_name") or "").strip()
        pv = _parse_mix_percentage_float(str(it.get("percentage") or ""))
        if nm and pv is not None:
            parsed_llm.append((nm, pv))

    if not parsed_llm:
        payload["revenue_by_segment"] = _fmp_segment_rows_to_mix_payload(rows)
        payload["data_source_label"] = (
            (payload.get("data_source_label") or "").rstrip()
            + " 业务线占比无法解析为有效百分比时，已使用 FMP Revenue Product Segmentation（"
            f"{filing_year} 财年）覆盖。"
        )
        _normalize_mix_lists(payload, idx_to_full=idx_to_full, allowed=allowed)
        return

    for r in rows:
        fmp_pct = float(r["percentage"])
        pair = _best_llm_match_for_fmp_segment(str(r["segment"]), parsed_llm)
        if pair is None:
            continue
        _, llm_pct = pair
        if abs(fmp_pct - llm_pct) > _FMP_SEGMENT_PCT_WARN_THRESHOLD:
            payload["validation_warning"] = _FMP_SEGMENT_VALIDATION_WARNING
            break


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
    从 SEC EDGAR 拉取 10-K 的 Item 1 / 1A / 7 / Item 8（分部附注节选），合并后调用 LLM 生成 ``BusinessProfile``。
    失败时抛出 ``ProfileGenerationError``（带可读中文说明）。
    """
    symbol = normalize_equity_ticker(ticker) or "UNKNOWN"
    filing_year = datetime.now(timezone.utc).year - 1
    _cached = get_step_cache(
        sector="__profile__",
        year=0,
        quarter=0,
        step="business_profile",
        ticker=symbol,
        cache_version=_PROFILE_CACHE_VERSION,
    )
    if _cached:
        try:
            return BusinessProfile.model_validate_json(_cached)
        except Exception:
            pass  # 缓存损坏，继续走原流程

    try:
        sections = get_10k_sections(symbol, filing_year)
    except SecEdgarError as e:
        raise ProfileGenerationError(f"无法获取 SEC 10-K 文本：{e}") from e

    merged = _merge_sections_for_profile(sections)
    if not merged.strip():
        raise ProfileGenerationError(
            "未能从 10-K 解析出任何可用章节文本（Item 1/1A/7/8）。"
        )

    excerpt = merged
    doc_uid = build_10k_profile_doc_uid(symbol, filing_year)
    chunks = split_into_paragraphs(excerpt)
    records = make_10k_records(doc_uid, chunks)
    replace_document_paragraphs(
        doc_uid,
        symbol,
        "10K_PROFILE_SECTIONS",
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
            timeout=120.0,
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
        f"SEC EDGAR 10-K 节选（标的 {symbol}，{filing_year} 公历年申报附近）："
        "Item 1 业务、Item 1A 风险、Item 7 MD&A、Item 8 附注（分部/地区营收相关窗口）；"
        f"合并顺序为 Item1→Item7→Item8 附注→Item1A；至多约 {_PROFILE_MERGED_MAX_CHARS} 字符送 OpenAI 抽取。"
        " 业务线占比与地理占比须区分：产品线入 revenue_by_segment，国家/区域入 revenue_by_geography。"
        " 管理层展望与行业判断主要来自 Item 7；原文未载则显示「原文未明确提及」。"
        " 关键原话须与原文逐字一致，否则会被系统丢弃。"
    )
    try:
        cik = get_cik(symbol)
        payload["primary_source_url"] = (
            f"https://www.sec.gov/cgi-bin/browse-edgar?action=getcompany&CIK={cik}&type=10-K&owner=exclude&count=40"
        )
    except SecEdgarError:
        payload["primary_source_url"] = f"https://www.sec.gov/edgar/search/#/q={symbol}"
    _normalize_mix_lists(payload, idx_to_full=idx_to_full, allowed=allowed)
    if _apply_item1_revenue_mix_fallback(payload, sections, records):
        payload["data_source_label"] = (
            (payload.get("data_source_label") or "").rstrip()
            + " 分部/地区营收占比若模型未返回，则自 Item 1 原文中的百分比列举规则解析补全（可点 📖 核对段落）。"
        )
        _normalize_mix_lists(payload, idx_to_full=idx_to_full, allowed=allowed)
    if _apply_item8_dollar_table_fallback(payload, sections, records):
        payload["data_source_label"] = (
            (payload.get("data_source_label") or "").rstrip()
            + " 若 Item 1 无散文式占比，则自 Item 7/8 中「Net sales by … (dollars in millions)」首列表格按金额推算占比。"
        )
        _normalize_mix_lists(payload, idx_to_full=idx_to_full, allowed=allowed)
    _apply_fmp_segment_validation(
        payload,
        symbol,
        filing_year,
        idx_to_full=idx_to_full,
        allowed=allowed,
    )
    # core_business FMP fallback：当10-K提取失败时，与 revenue_by_segment 同源（已由 FMP 校验/填充）
    cb = (payload.get("core_business") or "").strip()
    _cb_invalid_keywords = (
        "无法", "未提供", "未包含", "未见", "不可用", "原文未明确提及",
        "NOT_FOUND", "高管", "履历", "组织架构", "注册", "子公司运营",
    )
    _cb_is_invalid = (
        not cb
        or len(cb) < 30
        or any(kw in cb for kw in _cb_invalid_keywords)
    )
    if _cb_is_invalid:
        try:
            segs = payload.get("revenue_by_segment")
            if isinstance(segs, list) and segs:
                parts: list[str] = []
                for it in segs[:3]:
                    if not isinstance(it, dict):
                        continue
                    nm = str(it.get("segment_name") or "").strip()
                    if not nm:
                        continue
                    pct_raw = it.get("percentage")
                    pct_disp = ""
                    if pct_raw is not None:
                        p = str(pct_raw).strip()
                        if p.endswith("%"):
                            try:
                                num = float(p.replace("%", "").strip())
                                pct_disp = f"{num:.1f}%"
                            except ValueError:
                                pct_disp = p
                        else:
                            try:
                                num = float(p)
                                pct_disp = f"{num:.1f}%"
                            except (TypeError, ValueError):
                                pct_disp = p
                    parts.append(f"{nm}（{pct_disp}）" if pct_disp else nm)
                if parts:
                    seg_desc = "、".join(parts)
                    payload["core_business"] = (
                        f"{symbol} 主要业务分部包括：{seg_desc}等"
                        f"（数据来源：FMP Revenue Segmentation，10-K文本提取不可用）"
                    )
        except Exception:
            pass
    _strip_optional_supporting_quotes(payload)
    _apply_profile_text_fields(payload)
    check_profile_fields(payload, source_text=merged_excerpt)
    _normalize_field_sources(payload, idx_to_full=idx_to_full, allowed=allowed)
    _normalize_key_quotes(
        payload,
        merged_excerpt,
        idx_to_full=idx_to_full,
        allowed=allowed,
    )
    earnings_sp_map = _merge_earnings_quotations_into_profile(
        payload, symbol, filing_year
    )
    _normalize_corporate_actions(
        payload,
        merged_excerpt,
        idx_to_full=idx_to_full,
        allowed=allowed,
    )
    _append_news_corporate_actions_to_profile(payload, symbol)
    payload["document_uid"] = doc_uid
    cited = _collect_profile_cited_ids(payload)
    payload["source_paragraphs"] = {
        pid: id_to_text[pid] for pid in cited if pid in id_to_text
    }
    for pid, txt in earnings_sp_map.items():
        if pid in cited:
            payload["source_paragraphs"][pid] = txt

    try:
        _profile_obj = BusinessProfile.model_validate(payload)
        set_step_cache(
            sector="__profile__",
            year=0,
            quarter=0,
            step="business_profile",
            ticker=symbol,
            content=_profile_obj.model_dump_json(),
            cache_version=_PROFILE_CACHE_VERSION,
        )
        return _profile_obj
    except Exception as e:
        raise ProfileGenerationError(
            f"业务画像字段未通过校验（占比须含 % 等）：{e}"
        ) from e
