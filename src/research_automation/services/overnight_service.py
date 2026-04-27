"""隔夜速递：新闻时区下隔夜窗口内筛选 + 一句中文要点（LLM）；时间以 Finnhub Unix 优先。"""
from __future__ import annotations

import json
import re
from datetime import datetime
from typing import Any

from research_automation.core.news_time import (
    article_published_in_news_tz,
    filter_articles_in_half_open_window,
    get_news_timezone_name,
    overnight_window,
)
from research_automation.core.company_manager import get_active_tickers
from research_automation.extractors.finnhub_news import merge_finnhub_and_rss
from research_automation.extractors.llm_client import chat
from research_automation.extractors.news_client import (
    extract_tickers_from_text,
    fetch_rss_articles,
)
from research_automation.models.news import (
    CompanyNewsItem,
    MacroNewsItem,
    OvernightNewsResponse,
)
from research_automation.services.news_service import (
    NewsBriefError,
    fetch_company_news_raw_articles_for_tickers,
)


def _normalize_overnight_summary(text: str) -> str:
    """规范化模型输出：确保以「隔夜重点关注：」开头（漏写前缀时补齐）。"""
    s = (text or "").strip()
    if not s:
        return "隔夜重点关注：（模型未返回有效摘要，请查看下列条目。）"
    prefix = "隔夜重点关注："
    if s.startswith(prefix):
        return s
    if s.startswith("隔夜重点关注"):
        return prefix + s[len("隔夜重点关注") :].lstrip("：: ")
    return prefix + s


def _source_label(raw: str) -> str:
    """将原始 source 字段转为展示用短标签（空则视为 RSS）。"""
    s = (raw or "").strip()
    if not s:
        return "RSS"
    if s.startswith("Finnhub") or s.startswith("Benzinga") or s.startswith("RSS-"):
        return s
    return f"RSS-{s}"


RawArticle = dict[str, Any]


def _safe_json(reply: str) -> dict[str, Any]:
    s = (reply or "").strip()
    if not s:
        return {}
    try:
        obj = json.loads(s)
        return obj if isinstance(obj, dict) else {}
    except Exception:
        pass
    m = re.search(r"\{[\s\S]*\}", s)
    if m:
        try:
            obj = json.loads(m.group(0))
            return obj if isinstance(obj, dict) else {}
        except Exception:
            return {}
    return {}


def _build_overnight_prompt(
    macro_articles: list[RawArticle],
    company_articles: list[RawArticle],
    active_tickers: set[str],
    start: datetime,
    end: datetime,
) -> str:
    tz_label = get_news_timezone_name()
    window = f"{start.strftime('%Y-%m-%d %H:%M')} 至 {end.strftime('%Y-%m-%d %H:%M')} ({tz_label})"
    tickers_str = ", ".join(sorted(active_tickers)[:50])

    macro_lines = []
    for i, a in enumerate(macro_articles, 1):
        macro_lines.append(f"M{i}. [来源:{a.get('source','')}] {a.get('title','')} | {(a.get('description') or '')[:400]}")

    company_lines = []
    for i, a in enumerate(company_articles, 1):
        tickers = a.get("implied_tickers") or []
        company_lines.append(f"C{i}. [Ticker:{','.join(tickers)}] [来源:{a.get('source','')}] {a.get('title','')} | {(a.get('description') or '')[:400]}")

    return f"""你是专业财经编辑。时间窗口：{window}
监控Ticker池：{tickers_str}

【宏观新闻素材】（M编号）：
{chr(10).join(macro_lines) or '（无）'}

【公司新闻素材】（C编号）：
{chr(10).join(company_lines) or '（无）'}

任务：
1. 从宏观素材中选出**重点新闻**（地缘政治、央行/政策、领导人发言、重大经济数据），每条输出：
   - title：英文原标题（与输入完全一致）
   - summary：中文摘要（50-100字），只用已给信息，禁止编造
   - region：North America / Europe / Middle East / Asia / Global 五选一
   - source：来源
   - importance_score：1-10整数

2. 从公司素材中选出监控Ticker池内的重点新闻，每条输出：
   - ticker：最相关的一个大写ticker
   - title：英文原标题
   - summary：中文摘要（50-100字）
   - event_type：earnings/partnership/ma/buyback/insider_trade/management/research/other 八选一
   - source：来源
   - importance_score：1-10整数

3. 最后输出一句中文隔夜总结（overnight_summary），以"隔夜重点关注："开头。

只输出JSON，格式：
{{"macro_news":[{{"title":"...","summary":"...","region":"...","source":"...","importance_score":8}}],
"company_news":[{{"ticker":"AAPL","title":"...","summary":"...","event_type":"earnings","source":"...","importance_score":7}}],
"overnight_summary":"隔夜重点关注：..."}}

不要输出无关内容。不在监控Ticker池的公司新闻直接忽略。"""


def get_overnight_news(
    *,
    max_rss_items: int = 64,
    per_feed_limit: int = 12,
) -> OvernightNewsResponse:
    """
    拉取 RSS 与公司新闻（Benzinga 优先、Finnhub 兜底），合并去重后按「新闻时区昨天 16:00～今天 08:00」筛选；

    每条发布时间优先取 API Unix，否则用 RSS 的 UTC 时间；无有效时间则丢弃。
    使用 LLM 结构化输出宏观/公司隔夜要点。
    """
    rss_batch = fetch_rss_articles(
        max_items=max_rss_items,
        per_feed_limit=per_feed_limit,
    )
    start, end = overnight_window()
    from_d = start.date()
    to_d = end.date()
    company_raw = fetch_company_news_raw_articles_for_tickers(
        get_active_tickers(),
        from_d.isoformat(),
        to_d.isoformat(),
    )
    articles = merge_finnhub_and_rss(company_raw, rss_batch)
    if not articles:
        raise NewsBriefError(
            "未能从 RSS 与公司新闻源获取任何新闻（网络、密钥或 feed 不可用）。请稍后重试。"
        )

    filtered = filter_articles_in_half_open_window(articles, start, end)
    active = set(get_active_tickers())

    def _is_company_article(a: RawArticle) -> bool:
        implied = [str(x).strip().upper() for x in (a.get("implied_tickers") or [])]
        if any(t in active for t in implied):
            return True
        txt = f"{a.get('title') or ''} {a.get('description') or ''}"
        guessed = extract_tickers_from_text(txt)
        return any(t in active for t in guessed)

    macro_articles = [a for a in filtered if not _is_company_article(a)]
    company_articles = [a for a in filtered if _is_company_article(a)]

    if not filtered:
        return OvernightNewsResponse(
            overnight_summary=(
                "隔夜重点关注：本时间窗内未匹配到带有效发布时间的新闻条目；"
                "可能因网络延迟或各源未暴露时间戳，请稍后重试或结合下方完整晨报浏览。"
            ),
            macro_news=[],
            company_news=[],
            window_start_ny=start.isoformat(),
            window_end_ny=end.isoformat(),
            analyst_briefing="",
            provenance_note="",
        )

    prompt = _build_overnight_prompt(macro_articles, company_articles, active, start, end)
    try:
        reply = chat(prompt, response_format={"type": "json_object"}, timeout=120.0)
    except ValueError as e:
        raise NewsBriefError(f"语言模型未就绪：{e}") from e
    except RuntimeError as e:
        raise NewsBriefError(f"调用语言模型失败：{e}") from e

    payload = _safe_json(reply)
    macro_news = [MacroNewsItem(**item) for item in payload.get("macro_news", [])]
    company_news = [CompanyNewsItem(**item) for item in payload.get("company_news", [])]
    overnight_summary = _normalize_overnight_summary(payload.get("overnight_summary", "隔夜重点关注：无数据"))

    return OvernightNewsResponse(
        overnight_summary=overnight_summary,
        macro_news=macro_news,
        company_news=company_news,
        window_start_ny=start.isoformat(),
        window_end_ny=end.isoformat(),
        analyst_briefing="",
        provenance_note="",
    )
