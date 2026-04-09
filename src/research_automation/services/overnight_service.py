"""隔夜速递：新闻时区下隔夜窗口内筛选 + 一句中文要点（LLM）；时间以 Finnhub Unix 优先。"""
from __future__ import annotations

from datetime import datetime

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
from research_automation.models.news import OvernightNewsItem, OvernightNewsResponse
from research_automation.services.news_insights import (
    compute_news_insights,
    overnight_items_to_flat_dicts,
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


def _build_overnight_prompt(
    items: list[OvernightNewsItem],
    start: datetime,
    end: datetime,
) -> str:
    """拼装送往 LLM 的隔夜素材说明（含时间窗与编号条目）。"""
    lines: list[str] = []
    for i, it in enumerate(items, start=1):
        desc = (it.summary or "")[:640]
        lines.append(
            f"{i}. [来源:{it.source}] "
            f"标题:{it.title} "
            f"提要:{desc}"
        )
    tz_label = get_news_timezone_name()
    window = (
        f"{start.strftime('%Y-%m-%d %H:%M')} 至 {end.strftime('%Y-%m-%d %H:%M')} "
        f"({tz_label})"
    )
    body = "\n".join(lines)
    return f"""你是财经新闻编辑。以下为本地时间窗口 {window} 内、来自公司新闻源（Benzinga/Finnhub）与 RSS 的英文标题与提要摘录。

任务：用**恰好一句中文**概括隔夜对投研**最需一并扫一眼**的要点。
- 句式必须以「隔夜重点关注：」开头。
- 只综合已给出的标题与提要，禁止编造数字、未出现的实体或立场。

素材：
{body}

只输出这一句话，不要其他说明。"""


def get_overnight_news(
    *,
    max_rss_items: int = 64,
    per_feed_limit: int = 12,
) -> OvernightNewsResponse:
    """
    拉取 RSS 与公司新闻（Benzinga 优先、Finnhub 兜底），合并去重后按「新闻时区昨天 16:00～今天 08:00」筛选；

    每条发布时间优先取 API Unix，否则用 RSS 的 UTC 时间；无有效时间则丢弃。
    再调用 LLM 生成一句中文总结。公司源无数据时仍仅依赖 RSS，保持兼容。
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

    window_start = start.isoformat()
    window_end = end.isoformat()
    tz_name = get_news_timezone_name()

    filtered = filter_articles_in_half_open_window(articles, start, end)
    active = set(get_active_tickers())
    news_list: list[OvernightNewsItem] = []
    for a in filtered:
        title = (a.get("title") or "").strip()
        if not title:
            continue
        desc = (a.get("description") or "").strip()
        blob = f"{title} {desc}"
        tickers_set = set(extract_tickers_from_text(blob))
        for x in a.get("implied_tickers") or []:
            u = str(x).strip().upper()
            if u in active:
                tickers_set.add(u)
        tickers = sorted(tickers_set)
        pub_local = article_published_in_news_tz(a)
        pub_ny_s: str | None = pub_local.isoformat() if pub_local else None
        link = (a.get("link") or "").strip() or None
        news_list.append(
            OvernightNewsItem(
                title=title,
                summary=desc[:800] + ("…" if len(desc) > 800 else ""),
                source=_source_label(str(a.get("source") or "")),
                source_url=link,
                published_at_ny=pub_ny_s,
                matched_tickers=tickers,
            )
        )

    provenance = (
        "条目来自 Finnhub 公司新闻（须 FINNHUB_API_KEY）与 Reuters / Bloomberg / TechCrunch 等 RSS，"
        f"合并去重；时间窗以 {tz_name} 下「昨日 16:00～今日 08:00」为准，"
        "发布时间优先采用 Finnhub Unix 时间戳，否则回退 RSS。"
        "仅保留带有效发布时间的稿件。摘要由模型基于提要生成，请点原文核对。"
    )

    if not news_list:
        return OvernightNewsResponse(
            summary=(
                "隔夜重点关注：本时间窗内未匹配到带有效发布时间的新闻条目；"
                "可能因网络延迟或各源未暴露时间戳，请稍后重试或结合下方完整晨报浏览。"
            ),
            news_list=[],
            window_start_ny=window_start,
            window_end_ny=window_end,
            provenance_note=provenance,
            clusters=[],
            top_news=[],
            analyst_briefing="",
        )

    prompt = _build_overnight_prompt(news_list, start, end)
    try:
        reply = chat(prompt, timeout=90.0)
    except ValueError as e:
        raise NewsBriefError(f"语言模型未就绪：{e}") from e
    except RuntimeError as e:
        raise NewsBriefError(f"调用语言模型失败：{e}") from e

    summary = _normalize_overnight_summary(reply)

    # 聚类 / 评分 / 早评（与一句摘要分立；同一批新闻 24h 缓存）
    insight_date_key = f"{start.date().isoformat()}_{end.date().isoformat()}"
    flat_ins = overnight_items_to_flat_dicts(news_list)
    clusters, top_news, analyst_briefing, score_map = compute_news_insights(
        flat_ins,
        context="overnight",
        date_key=insight_date_key,
        monitor_tickers=sorted(active),
    )
    scored_list: list[OvernightNewsItem] = []
    for i, it in enumerate(news_list):
        sc = score_map.get(i, 5)
        scored_list.append(it.model_copy(update={"importance_score": sc}))

    return OvernightNewsResponse(
        summary=summary,
        news_list=scored_list,
        window_start_ny=window_start,
        window_end_ny=window_end,
        provenance_note=provenance,
        clusters=clusters,
        top_news=top_news,
        analyst_briefing=analyst_briefing,
    )
