"""隔夜速递：纽约时段窗口内 RSS 筛选 + 一句中文要点（LLM）。"""
from __future__ import annotations

from datetime import datetime, timedelta, time, timezone
from zoneinfo import ZoneInfo

from research_automation.extractors.llm_client import chat
from research_automation.extractors.news_client import (
    RawArticle,
    extract_tickers_from_text,
    fetch_rss_articles,
    parse_published_at_utc,
)
from research_automation.models.news import OvernightNewsItem, OvernightNewsResponse
from research_automation.services.news_service import NewsBriefError

_NY = ZoneInfo("America/New_York")


def overnight_window_ny(
    *,
    now_ny: datetime | None = None,
) -> tuple[datetime, datetime]:
    """
    纽约本地日历：「昨天 16:00」至「今天 08:00」半开区间 [start, end)。

    ``now_ny`` 仅用于测试注入；默认 ``datetime.now(America/New_York)``。
    """
    now = now_ny or datetime.now(_NY)
    if now.tzinfo is None:
        now = now.replace(tzinfo=_NY)
    else:
        now = now.astimezone(_NY)
    d = now.date()
    start = datetime.combine(d - timedelta(days=1), time(16, 0), tzinfo=_NY)
    end = datetime.combine(d, time(8, 0), tzinfo=_NY)
    return start, end


def _normalize_overnight_summary(text: str) -> str:
    """确保以「隔夜重点关注：」开头（模型偶发漏前缀时补齐）。"""
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
    s = (raw or "").strip()
    if not s:
        return "RSS"
    return s if str(s).startswith("RSS-") else f"RSS-{s}"


def _filter_articles_in_window(
    articles: list[RawArticle],
    start: datetime,
    end: datetime,
) -> list[RawArticle]:
    """保留发布时间落在 [start, end)（纽约）内的条目；无时间戳的条目丢弃。"""
    out: list[RawArticle] = []
    for a in articles:
        iso = a.get("published_at_utc")
        if not iso:
            continue
        try:
            pub_utc = parse_published_at_utc(str(iso))
            pub_ny = pub_utc.astimezone(_NY)
        except (TypeError, ValueError):
            continue
        if start <= pub_ny < end:
            out.append(a)

    def _sort_key(a: RawArticle) -> datetime:
        p = a.get("published_at_utc")
        if not p:
            return datetime.min.replace(tzinfo=timezone.utc)
        try:
            return parse_published_at_utc(str(p))
        except (TypeError, ValueError):
            return datetime.min.replace(tzinfo=timezone.utc)

    out.sort(key=_sort_key, reverse=True)
    return out


def _build_overnight_prompt(
    items: list[OvernightNewsItem],
    start: datetime,
    end: datetime,
) -> str:
    lines: list[str] = []
    for i, it in enumerate(items, start=1):
        desc = (it.summary or "")[:640]
        lines.append(
            f"{i}. [来源:{it.source}] "
            f"标题:{it.title} "
            f"提要:{desc}"
        )
    window = (
        f"{start.strftime('%Y-%m-%d %H:%M')} 至 {end.strftime('%Y-%m-%d %H:%M')} "
        f"(America/New_York)"
    )
    body = "\n".join(lines)
    return f"""你是财经新闻编辑。以下为纽约时间窗口 {window} 内、来自公开 RSS 的英文标题与提要摘录。

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
    实时拉取 RSS，按「纽约昨天 16:00～今天 08:00」筛选，调用 LLM 生成一句中文总结。

    无带时间戳且落在窗口内的条目时，``news_list`` 为空，``summary`` 为说明性文案（仍可 HTTP 200）。
    """
    articles = fetch_rss_articles(
        max_items=max_rss_items,
        per_feed_limit=per_feed_limit,
    )
    if not articles:
        raise NewsBriefError(
            "未能从 RSS 获取任何新闻（网络、反爬或 feed 不可用）。请稍后重试。"
        )

    start, end = overnight_window_ny()
    window_start = start.isoformat()
    window_end = end.isoformat()

    filtered = _filter_articles_in_window(articles, start, end)
    news_list: list[OvernightNewsItem] = []
    for a in filtered:
        title = (a.get("title") or "").strip()
        if not title:
            continue
        desc = (a.get("description") or "").strip()
        blob = f"{title} {desc}"
        tickers = extract_tickers_from_text(blob)
        iso = a.get("published_at_utc")
        pub_ny_s: str | None = None
        if iso:
            try:
                pub_ny_s = (
                    parse_published_at_utc(str(iso))
                    .astimezone(_NY)
                    .replace(microsecond=0)
                    .isoformat()
                )
            except (TypeError, ValueError):
                pub_ny_s = None
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
        "条目来自 Reuters / Bloomberg / TechCrunch 等公开 RSS，时间窗以 "
        "America/New_York 昨天 16:00～今天 08:00 为准；"
        "仅保留 RSS 提供有效发布时间的稿件。摘要由模型基于提要生成，请点原文核对。"
    )

    if not news_list:
        return OvernightNewsResponse(
            summary=(
                "隔夜重点关注：本时间窗内当前 RSS 批次未匹配到带有效发布时间的新闻条目；"
                "可能因网络延迟或各源未暴露时间戳，请稍后重试或结合下方完整晨报浏览。"
            ),
            news_list=[],
            window_start_ny=window_start,
            window_end_ny=window_end,
            provenance_note=provenance,
        )

    prompt = _build_overnight_prompt(news_list, start, end)
    try:
        reply = chat(prompt, timeout=90.0)
    except ValueError as e:
        raise NewsBriefError(f"语言模型未就绪：{e}") from e
    except RuntimeError as e:
        raise NewsBriefError(f"调用语言模型失败：{e}") from e

    summary = _normalize_overnight_summary(reply)

    return OvernightNewsResponse(
        summary=summary,
        news_list=news_list,
        window_start_ny=window_start,
        window_end_ny=window_end,
        provenance_note=provenance,
    )
