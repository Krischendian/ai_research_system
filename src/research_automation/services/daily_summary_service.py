"""昨日总结：纽约日历「昨日」全天 RSS 筛选 + LLM 宏观/公司主题归类。"""
from __future__ import annotations

import json
import re
from datetime import datetime, timedelta, time, timezone
from typing import Any

from zoneinfo import ZoneInfo

from research_automation.extractors.llm_client import chat
from research_automation.extractors.news_client import (
    RawArticle,
    fetch_rss_articles,
    parse_published_at_utc,
)
from research_automation.models.news import YesterdaySummaryResponse, YesterdayThemeGroup
from research_automation.services.news_service import NewsBriefError

_NY = ZoneInfo("America/New_York")


def yesterday_calendar_window_ny(
    *,
    now_ny: datetime | None = None,
) -> tuple[datetime, datetime]:
    """
    纽约本地日历「昨天」的 00:00 至「今天」00:00，即昨日全天 [start, end)。
    与「00:00–23:59」等价（半开区间上界为今日 0 点）。
    """
    now = now_ny or datetime.now(_NY)
    if now.tzinfo is None:
        now = now.replace(tzinfo=_NY)
    else:
        now = now.astimezone(_NY)
    today_d = now.date()
    yday_d = today_d - timedelta(days=1)
    start = datetime.combine(yday_d, time(0, 0), tzinfo=_NY)
    end = datetime.combine(today_d, time(0, 0), tzinfo=_NY)
    return start, end


def _source_label(raw: str) -> str:
    s = (raw or "").strip()
    if not s:
        return "RSS"
    return s if str(s).startswith("RSS-") else f"RSS-{s}"


def _filter_yesterday_articles(
    articles: list[RawArticle],
    start: datetime,
    end: datetime,
) -> list[RawArticle]:
    """保留发布时刻落在纽约昨日 [start, end) 的 RSS 条目。"""
    out: list[RawArticle] = []
    for a in articles:
        iso = a.get("published_at_utc")
        if not iso:
            continue
        try:
            pub_ny = parse_published_at_utc(str(iso)).astimezone(_NY)
        except (TypeError, ValueError):
            continue
        if start <= pub_ny < end:
            out.append(a)

    def _sk(x: RawArticle) -> datetime:
        p = x.get("published_at_utc")
        if not p:
            return datetime.min.replace(tzinfo=timezone.utc)
        try:
            return parse_published_at_utc(str(p))
        except (TypeError, ValueError):
            return datetime.min.replace(tzinfo=timezone.utc)

    out.sort(key=_sk, reverse=True)
    return out


def _safe_json_obj(raw: str) -> dict[str, Any]:
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
    body_lines: list[str] = []
    for i, a in enumerate(filtered, start=1):
        desc = (a.get("description") or "")[:520]
        body_lines.append(
            f"{i}. [来源:{_source_label(str(a.get('source') or ''))}] "
            f"标题:{(a.get('title') or '').strip()} "
            f"提要:{desc}"
        )
    body = "\n".join(body_lines)
    return f"""你是财经编辑。以下为纽约本地日期 {date_label} 当日（全天 America/New_York）内、公开 RSS 的英文简讯（编号 1 至 {n}）。

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
    拉取 RSS，筛出纽约「昨日」全天内有发布时间的条目，经 LLM 做宏观/公司主题归类与 Markdown 汇总。
    """
    articles = fetch_rss_articles(
        max_items=max_rss_items,
        per_feed_limit=per_feed_limit,
    )
    if not articles:
        raise NewsBriefError(
            "未能从 RSS 获取任何新闻（网络、反爬或 feed 不可用）。请稍后重试。"
        )

    start, end = yesterday_calendar_window_ny()
    window_start = start.isoformat()
    window_end = end.isoformat()
    date_label = start.strftime("%Y-%m-%d")

    filtered = _filter_yesterday_articles(articles, start, end)
    n = len(filtered)

    provenance = (
        f"纽约昨日日历日 {date_label}（{window_start}–{window_end}，America/New_York）；"
        "仅含 RSS 提供有效发布时间的条目，实时 feed 不一定覆盖当日全部报道。"
        "主题与条数由模型基于标题/提要归类，请核对原文。"
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

    return YesterdaySummaryResponse(
        markdown=markdown,
        macro=macro_g,
        company=company_g,
        articles_in_window=n,
        window_start_ny=window_start,
        window_end_ny=window_end,
        provenance_note=provenance,
    )
