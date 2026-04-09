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
from research_automation.extractors.news_client import RawArticle, fetch_rss_articles
from research_automation.models.news import YesterdaySummaryResponse, YesterdayThemeGroup
from research_automation.services.news_insights import (
    compute_news_insights,
    raw_articles_with_tickers_to_flat,
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


def _normalize_groups(
    macro_raw: Any,
    company_raw: Any,
    n: int,
) -> tuple[list[YesterdayThemeGroup], list[YesterdayThemeGroup]]:
    """将模型输出的主题合并去重编号，避免重复/漏号；漏号并入「其他要闻」。"""
    used: set[int] = set()
    macro_out: list[YesterdayThemeGroup] = []
    company_out: list[YesterdayThemeGroup] = []

    def _take_indices(idxs: Any) -> list[int]:
        if not isinstance(idxs, list):
            return []
        out: list[int] = []
        for x in idxs:
            try:
                i = int(x)
            except (TypeError, ValueError):
                continue
            if 1 <= i <= n and i not in used:
                out.append(i)
                used.add(i)
        return out

    for it in macro_raw if isinstance(macro_raw, list) else []:
        if not isinstance(it, dict):
            continue
        topic = str(it.get("topic", "")).strip()
        idxs = _take_indices(it.get("article_indices"))
        if topic and idxs:
            macro_out.append(
                YesterdayThemeGroup(
                    topic=topic, count=len(idxs), article_indices=idxs, tickers=[]
                )
            )

    for it in company_raw if isinstance(company_raw, list) else []:
        if not isinstance(it, dict):
            continue
        topic = str(it.get("topic", "")).strip()
        idxs = _take_indices(it.get("article_indices"))
        tickers_raw = it.get("tickers") or []
        tickers: list[str] = []
        if isinstance(tickers_raw, list):
            tickers = sorted(
                {str(t).strip().upper() for t in tickers_raw if str(t).strip()}
            )
        if topic and idxs:
            company_out.append(
                YesterdayThemeGroup(
                    topic=topic, count=len(idxs), article_indices=idxs, tickers=tickers
                )
            )

    orphans = [i for i in range(1, n + 1) if i not in used]
    if orphans:
        macro_out.append(
            YesterdayThemeGroup(
                topic="其他要闻",
                count=len(orphans),
                article_indices=orphans,
                tickers=[],
            )
        )
    return macro_out, company_out


def _build_markdown(
    macro: list[YesterdayThemeGroup],
    company: list[YesterdayThemeGroup],
) -> str:
    """由结构化主题列表生成 Markdown 简报（宏观 / 公司两节）。"""
    lines = ["## 宏观", ""]
    if not macro:
        lines.append("- （无）")
    else:
        for g in macro:
            lines.append(f"- {g.topic}（{g.count} 条新闻）")
    lines.extend(["", "## 公司", ""])
    if not company:
        lines.append("- （无）")
    else:
        for g in company:
            lines.append(f"- {g.topic}（{g.count} 条）")
    return "\n".join(lines)


def _build_llm_prompt(filtered: list[RawArticle], *, date_label: str, n: int) -> str:
    """拼装昨日全文新闻列表与归类任务说明，供 LLM 输出 JSON。"""
    tz_name = get_news_timezone_name()
    body_lines: list[str] = []
    for i, a in enumerate(filtered, start=1):
        desc = (a.get("description") or "")[:520]
        body_lines.append(
            f"{i}. [来源:{_source_label(str(a.get('source') or ''))}] "
            f"标题:{(a.get('title') or '').strip()} "
            f"提要:{desc}"
        )
    body = "\n".join(body_lines)
    return f"""你是财经编辑。以下为本地日期 {date_label} 当日（全天 {tz_name}）内、公司新闻源（Benzinga/Finnhub）与 RSS 合并后的英文简讯（编号 1 至 {n}）。

任务：
1. 将每一条编号**恰好归入一类**：「宏观」下的某一个主题，或「公司」下的某一个主题；编号 1..{n} 必须**各出现一次**，不得遗漏或重复。
2. 「宏观」：货币政策/利率、地缘与政策、大宗与汇率、市场全景、多主体行业动态等。
3. 「公司」：围绕**单一明确公司**的事件；可在 JSON 里给 tickers 数组（大写代码如 AAPL）。
4. 只输出一个 JSON 对象，格式示例：
{{"macro":[{{"topic":"中文主题短语","article_indices":[1,2]}}],"company":[{{"topic":"中文主题","article_indices":[3],"tickers":["AAPL"]}}]}}
5. topic 要短；article_indices 为整数数组。

简讯：
{body}
"""


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
    n = len(filtered)

    provenance = (
        f"昨日日历日 {date_label}（{window_start}–{window_end}，{tz_name}）；"
        "含 Benzinga/Finnhub 公司新闻与 RSS 合并去重；发布时间优先 API Unix，否则 RSS；"
        "仅统计带有效发布时间的条目。主题归类与聚类/评分/早评为独立模型调用；请核对原文。"
    )

    if n == 0:
        md = _build_markdown([], [])
        return YesterdaySummaryResponse(
            markdown=md,
            macro=[],
            company=[],
            articles_in_window=0,
            window_start_ny=window_start,
            window_end_ny=window_end,
            provenance_note=provenance,
            clusters=[],
            top_news=[],
            analyst_briefing="",
        )

    prompt = _build_llm_prompt(filtered, date_label=date_label, n=n)
    try:
        reply = chat(
            prompt,
            response_format={"type": "json_object"},
            timeout=120.0,
        )
    except ValueError as e:
        raise NewsBriefError(f"语言模型未就绪：{e}") from e
    except RuntimeError as e:
        raise NewsBriefError(f"调用语言模型失败：{e}") from e

    try:
        payload = _safe_json_obj(reply)
    except (json.JSONDecodeError, ValueError) as e:
        raise NewsBriefError(f"模型返回无法解析为 JSON：{e}") from e

    macro_g, company_g = _normalize_groups(
        payload.get("macro"),
        payload.get("company"),
        n,
    )
    markdown = _build_markdown(macro_g, company_g)

    # 聚类 / 重要性 / 分析师早评（单次 LLM，24h 缓存；与主题归类分离）
    active = set(get_active_tickers())
    flat_ins = raw_articles_with_tickers_to_flat(filtered, active)
    logger.info("昨日总结 洞察输入条数=%d（时间窗内）", len(flat_ins))
    clusters, top_news, analyst_briefing, _ = compute_news_insights(
        flat_ins,
        context="yesterday_summary",
        date_key=date_label,
        monitor_tickers=sorted(active),
    )

    return YesterdaySummaryResponse(
        markdown=markdown,
        macro=macro_g,
        company=company_g,
        articles_in_window=n,
        window_start_ny=window_start,
        window_end_ny=window_end,
        provenance_note=provenance,
        clusters=clusters,
        top_news=top_news,
        analyst_briefing=analyst_briefing,
    )
