"""昨日总结：新闻时区「昨日」全天筛选 + LLM 宏观/公司主题归类；时间以 Finnhub Unix 优先。"""
from __future__ import annotations

import json
import logging
import re
from typing import Any

from research_automation.core.news_time import (
    filter_articles_in_half_open_window,
    get_news_timezone_name,
    yesterday_full_day_window,
)
from research_automation.core.company_manager import get_active_tickers
from research_automation.extractors.finnhub_news import merge_finnhub_and_rss
from research_automation.extractors.llm_client import chat
from research_automation.extractors.news_client import (
    RawArticle,
    extract_tickers_from_text,
    fetch_rss_articles,
)
from research_automation.models.news import (
    CompanyNewsItem,
    MacroNewsItem,
    YesterdaySummaryResponse,
)
from research_automation.services.news_service import (
    NewsBriefError,
    fetch_company_news_raw_articles_for_tickers,
)

logger = logging.getLogger(__name__)


def _source_label(raw: str) -> str:
    """将原始 source 字段转为展示用短标签（空则视为 RSS）。"""
    s = (raw or "").strip()
    if not s:
        return "RSS"
    if s.startswith("Finnhub") or s.startswith("Benzinga") or s.startswith("RSS-"):
        return s
    return f"RSS-{s}"


def _safe_json_obj(raw: str) -> dict[str, Any]:
    """从模型回复中剥离 Markdown 代码块后解析 JSON 对象，失败则抛出异常。"""
    text = (raw or "").strip()
    m = re.search(r"```(?:json)?\s*([\s\S]*?)\s*```", text)
    if m:
        text = m.group(1).strip()
    try:
        data = json.loads(text)
    except json.JSONDecodeError:
        s = text.find("{")
        e = text.rfind("}")
        if s == -1 or e <= s:
            raise
        data = json.loads(text[s : e + 1])
    if not isinstance(data, dict):
        raise ValueError("JSON 根须为对象")
    return data


def _build_llm_prompt(
    macro_articles: list[RawArticle],
    company_articles: list[RawArticle],
    active_tickers: set[str],
    date_label: str,
) -> str:
    tickers_str = ", ".join(sorted(active_tickers)[:50])
    macro_lines = [f"M{i}. [来源:{a.get('source','')}] {a.get('title','')} | {(a.get('description') or '')[:400]}"
                   for i, a in enumerate(macro_articles, 1)]
    company_lines = [f"C{i}. [Ticker:{','.join(a.get('implied_tickers') or [])}] {a.get('title','')} | {(a.get('description') or '')[:400]}"
                     for i, a in enumerate(company_articles, 1)]

    return f"""你是专业财经编辑。日期：{date_label}（纽约时间全天）
监控Ticker池：{tickers_str}

【宏观素材】：
{chr(10).join(macro_lines) or '（无）'}

【公司素材】：
{chr(10).join(company_lines) or '（无）'}

任务与输出格式同隔夜版本（macro_news含region，company_news含ticker+event_type）。
重点选出当日真正重要的新闻，宏观重点关注北美/欧洲/中东/亚洲的地缘政治、央行、政策、领导人发言；
公司只选监控Ticker池内有实质影响的事件。

输出JSON格式：
{{"macro_news":[...],"company_news":[...]}}"""


def get_yesterday_summary(
    *,
    max_rss_items: int = 120,
    per_feed_limit: int = 20,
) -> YesterdaySummaryResponse:
    """
    拉取 RSS 与公司新闻（Benzinga 优先、Finnhub 兜底），合并去重后筛出新闻时区「昨日」全天内有有效发布时间的条目；

    发布时间优先 API 返回的 Unix，否则 RSS；公司源为空时仍可仅用 RSS。
    再经 LLM 做宏观/公司主题归类并生成 Markdown。
    """
    rss_batch = fetch_rss_articles(
        max_items=max_rss_items,
        per_feed_limit=per_feed_limit,
    )
    start, end = yesterday_full_day_window()
    yday = start.date().isoformat()
    company_raw = fetch_company_news_raw_articles_for_tickers(
        get_active_tickers(),
        yday,
        yday,
    )
    if not rss_batch and not company_raw:
        raise NewsBriefError(
            "未能从 RSS 与公司新闻源获取任何新闻（网络、密钥或 feed 不可用）。请稍后重试。"
        )
    articles = merge_finnhub_and_rss(company_raw, rss_batch)
    window_start = start.isoformat()
    window_end = end.isoformat()
    date_label = start.strftime("%Y-%m-%d")
    tz_name = get_news_timezone_name()

    filtered = filter_articles_in_half_open_window(articles, start, end)
    provenance = (
        f"昨日日历日 {date_label}（{window_start}–{window_end}，{tz_name}）；"
        "含 Benzinga/Finnhub 公司新闻与 RSS 合并去重；发布时间优先 API Unix，否则 RSS；"
        "仅统计带有效发布时间的条目。结构化摘要由模型生成，请核对原文。"
    )

    if not filtered:
        return YesterdaySummaryResponse(
            macro_news=[],
            company_news=[],
            articles_in_window=0,
            window_start_ny=window_start,
            window_end_ny=window_end,
            provenance_note=provenance,
            analyst_briefing="",
        )

    active = set(get_active_tickers())

    def _is_company_article(a: RawArticle) -> bool:
        implied = [str(x).strip().upper() for x in (a.get("implied_tickers") or [])]
        if any(t in active for t in implied):
            return True
        txt = f"{a.get('title') or ''} {a.get('description') or ''}"
        guessed = extract_tickers_from_text(txt)
        return any(t in active for t in guessed)

    def _is_company_source(a: RawArticle) -> bool:
        return _is_company_article(a)

    # 限制送入LLM的条数，防止JSON截断
    macro_articles = [a for a in filtered if not _is_company_source(a)][:25]
    company_articles = [a for a in filtered if _is_company_source(a)][:25]

    prompt = _build_llm_prompt(
        macro_articles,
        company_articles,
        active,
        date_label,
    )
    try:
        reply = chat(
            prompt,
            response_format={"type": "json_object"},
            timeout=120.0,
            max_tokens=4000,
        )
    except ValueError as e:
        raise NewsBriefError(f"语言模型未就绪：{e}") from e
    except RuntimeError as e:
        raise NewsBriefError(f"调用语言模型失败：{e}") from e

    try:
        payload = _safe_json_obj(reply)
    except (json.JSONDecodeError, ValueError) as e:
        raise NewsBriefError(f"模型返回无法解析为 JSON：{e}") from e

    macro_news = [MacroNewsItem(**item) for item in payload.get("macro_news", [])]
    company_news = [CompanyNewsItem(**item) for item in payload.get("company_news", [])]

    return YesterdaySummaryResponse(
        macro_news=macro_news,
        company_news=company_news,
        articles_in_window=len(filtered),
        window_start_ny=window_start,
        window_end_ny=window_end,
        analyst_briefing="",
        provenance_note=provenance,
    )
