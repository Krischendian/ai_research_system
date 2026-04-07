"""晨报：RSS 原文 + LLM 摘要与宏观/公司分类。"""
from __future__ import annotations

import json
import re
from typing import Any

from research_automation.extractors.llm_client import chat
from research_automation.extractors.news_client import (
    RawArticle,
    extract_tickers_from_text,
    fetch_rss_articles,
)
from research_automation.models.news import MorningBrief, NewsItem


class NewsBriefError(Exception):
    """晨报生成失败（可映射为 HTTP 说明）。"""

    def __init__(self, message: str) -> None:
        self.message = message
        super().__init__(message)


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


def _build_prompt(articles: list[RawArticle]) -> str:
    lines: list[str] = []
    for i, a in enumerate(articles, start=1):
        desc = (a.get("description") or "")[:800]
        lines.append(
            f"{i}. [来源:{a.get('source','')}] 标题:{a.get('title','')} 提要:{desc}"
        )
    return "\n".join(lines)


def _text_for_ticker_match(item: NewsItem, articles: list[RawArticle]) -> str:
    """合并标题、摘要及可关联到的 RSS 提要，供 ticker 匹配。"""
    parts: list[str] = [item.title or "", item.summary or ""]
    t = item.title.strip().lower()
    if not t:
        return " ".join(parts)
    for a in articles:
        at = a["title"].strip().lower()
        if not at:
            continue
        if t == at or t in at or at in t:
            parts.append(a.get("description") or "")
            break
    return " ".join(parts)


def _match_source_url(title: str, articles: list[RawArticle]) -> str | None:
    """按标题将 LLM 输出与 RSS 原文关联，获取可点击链接。"""
    t = title.strip().lower()
    if not t:
        return None
    for a in articles:
        if a["title"].strip().lower() == t:
            link = (a.get("link") or "").strip()
            return link or None
    for a in articles:
        at = a["title"].strip().lower()
        if t in at or at in t:
            link = (a.get("link") or "").strip()
            if link:
                return link
    return None


def _finalize_news_items(items: list[NewsItem], articles: list[RawArticle]) -> list[NewsItem]:
    """补齐原文链接与 ``matched_tickers``（基于监控池关键词匹配）。"""
    out: list[NewsItem] = []
    for it in items:
        blob = _text_for_ticker_match(it, articles)
        tickers = extract_tickers_from_text(blob)
        url = _match_source_url(it.title, articles)
        out.append(
            it.model_copy(update={"matched_tickers": tickers, "source_url": url})
        )
    return out


def _prioritize_articles_with_tickers(articles: list[RawArticle]) -> list[RawArticle]:
    """把标题/提要已命中监控池的条目排在前面，利于 LLM 归入公司类。"""
    hits: list[RawArticle] = []
    rest: list[RawArticle] = []
    for a in articles:
        blob = f"{a.get('title') or ''} {a.get('description') or ''}"
        if extract_tickers_from_text(blob):
            hits.append(a)
        else:
            rest.append(a)
    return hits + rest


def _fallback_company_from_rss(articles: list[RawArticle], *, limit: int = 12) -> list[NewsItem]:
    """
    当 LLM 路径下公司新闻为空时，直接用 RSS 英文提要生成卡片（仍须命中监控池 ticker）。
    摘要带「RSS 提要」前缀，避免与模型润色后的宏观区混淆期望。
    """
    out: list[NewsItem] = []
    seen: set[str] = set()
    for a in articles:
        title = (a.get("title") or "").strip()
        if not title:
            continue
        desc = (a.get("description") or "").strip()
        blob = f"{title} {desc}"
        tickers = extract_tickers_from_text(blob)
        if not tickers:
            continue
        key = title.lower()
        if key in seen:
            continue
        seen.add(key)
        excerpt = desc[:520] + ("…" if len(desc) > 520 else "")
        summary = (
            f"【RSS 提要，原文摘录】{excerpt}"
            if excerpt
            else "【RSS】请结合英文标题与下方原文链接阅读。"
        )
        src = a.get("source") or "RSS"
        src_field = src if str(src).startswith("RSS-") else f"RSS-{src}"
        link = (a.get("link") or "").strip() or None
        out.append(
            NewsItem(
                title=title,
                summary=summary,
                source=src_field,
                source_url=link,
                matched_tickers=tickers,
            )
        )
        if len(out) >= limit:
            break
    return out


def get_morning_brief(
    *,
    max_rss_items: int = 32,
) -> MorningBrief:
    """
    抓取 Reuters/Bloomberg RSS，调用 LLM 生成中文摘要并分为宏观 / 公司两类。

    - 不得捏造 RSS 中未出现的事实；摘要须严格基于给定标题与提要。
    - 输出须符合 ``MorningBrief`` JSON 结构。
    """
    articles = fetch_rss_articles(max_items=max_rss_items, per_feed_limit=7)
    if not articles:
        raise NewsBriefError(
            "未能从 RSS 获取任何新闻（网络、反爬或 feed 不可用）。请稍后重试。"
        )

    articles = _prioritize_articles_with_tickers(articles)
    body = _build_prompt(articles)
    prompt = f"""你是财经新闻编辑助手。下面是同源 RSS 抓取的多条英文简讯（带标题与提要）。

任务：
1. 为每条单独生成**中文摘要**（60～120 字），只综合已给出的标题与提要，禁止编造数字、机构立场或 RSS 未出现的信息。
2. 将每条归类为二选一：
   - **宏观**：央行/利率/通胀、地缘与政策、大宗商品与汇率、市场整体/指数层面、国际机构宏观展望等与**非单一公司**强相关；
   - **公司**：具体企业财报/指引、并购、管理层变动、单公司产品与订单、针对**明确公司主体**的事件。
3. 每条输出字段：title 使用**原文英文标题**（与输入一致，勿自行改写事实性信息）、summary（你的中文摘要）、source（格式为「RSS-来源标签」，与输入中的来源一致，例如「RSS-Reuters」或「RSS-Bloomberg」）。

4. 只输出一个 JSON 对象，键为 macro_news 与 company_news，值均为数组，元素形状：{{"title","summary","source"}}。
5. 若某条可同时偏向两类，选更主要的一类；两大类总数不必与输入条数相等（可合并极度过短的重复项或不适用项丢弃），但**至少应覆盖输入中多数独立新闻**。

输入简讯：
{body}
"""

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
        payload = _extract_json_object(reply)
    except (json.JSONDecodeError, ValueError) as e:
        raise NewsBriefError(
            f"模型返回无法解析为 JSON，请稍后重试。详情：{e}"
        ) from e

    try:
        macro_raw = payload.get("macro_news") or []
        company_raw = payload.get("company_news") or []
        if not isinstance(macro_raw, list) or not isinstance(company_raw, list):
            raise ValueError("macro_news / company_news 须为数组")

        def _as_items(xs: list[Any]) -> list[NewsItem]:
            out: list[NewsItem] = []
            for it in xs:
                if not isinstance(it, dict):
                    continue
                t = str(it.get("title", "")).strip()
                s = str(it.get("summary", "")).strip()
                src = str(it.get("source", "")).strip()
                if not t or not s:
                    continue
                if not src:
                    src = "RSS"
                out.append(NewsItem(title=t, summary=s, source=src))
            return out

        macro = _finalize_news_items(_as_items(macro_raw), articles)
        company_in = _finalize_news_items(_as_items(company_raw), articles)
        # 公司类：LLM 标为公司且命中监控池 ticker 的条目
        company = [it for it in company_in if it.matched_tickers]
        # 宏观里若摘要/标题已命中 ticker，也并入公司版块（宏观列表仍全部保留，不去重删除）
        seen_company = {it.title.strip().lower() for it in company}
        for it in macro:
            if not it.matched_tickers:
                continue
            key = it.title.strip().lower()
            if key and key not in seen_company:
                seen_company.add(key)
                company.append(it)

        extra_src = ""
        if not company:
            fb = _fallback_company_from_rss(articles)
            if fb:
                company = fb
                extra_src = "；公司新闻含 RSS 直配条目（提要来自英文 RSS，未再经模型写中文摘要）。"

        if not macro and not company:
            raise ValueError("模型未返回有效新闻条目")

        return MorningBrief(
            macro_news=macro,
            company_news=company,
            data_source_label=(
                "Reuters / Bloomberg / TechCrunch 等公开 RSS（各条附「原文链接」）"
                " + OpenAI 中文摘要与宏观/公司分类"
                f"{extra_src}"
            ),
            provenance_note=(
                "摘要由模型基于 RSS 提要生成，可能与报道原文不一致；"
                "请务必点击原文链接核对，不构成任何投资建议。"
            ),
        )
    except Exception as e:
        raise NewsBriefError(f"晨报结构校验失败：{e}") from e
