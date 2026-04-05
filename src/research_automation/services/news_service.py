"""晨报：RSS 原文 + LLM 摘要与宏观/公司分类。"""
from __future__ import annotations

import json
import re
from typing import Any

from research_automation.extractors.llm_client import chat
from research_automation.extractors.news_client import RawArticle, fetch_rss_articles
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


def _with_source_urls(items: list[NewsItem], articles: list[RawArticle]) -> list[NewsItem]:
    return [
        it.model_copy(update={"source_url": _match_source_url(it.title, articles)})
        for it in items
    ]


def get_morning_brief(
    *,
    max_rss_items: int = 20,
) -> MorningBrief:
    """
    抓取 Reuters/Bloomberg RSS，调用 LLM 生成中文摘要并分为宏观 / 公司两类。

    - 不得捏造 RSS 中未出现的事实；摘要须严格基于给定标题与提要。
    - 输出须符合 ``MorningBrief`` JSON 结构。
    """
    articles = fetch_rss_articles(max_items=max_rss_items, per_feed_limit=8)
    if not articles:
        raise NewsBriefError(
            "未能从 RSS 获取任何新闻（网络、反爬或 feed 不可用）。请稍后重试。"
        )

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

        macro = _as_items(macro_raw)
        company = _as_items(company_raw)
        if not macro and not company:
            raise ValueError("模型未返回有效新闻条目")

        return MorningBrief(
            macro_news=_with_source_urls(macro, articles),
            company_news=_with_source_urls(company, articles),
            data_source_label=(
                "Reuters / Bloomberg 等公开 RSS（各条附「原文链接」）"
                " + OpenAI 中文摘要与宏观/公司分类"
            ),
            provenance_note=(
                "摘要由模型基于 RSS 提要生成，可能与报道原文不一致；"
                "请务必点击原文链接核对，不构成任何投资建议。"
            ),
        )
    except Exception as e:
        raise NewsBriefError(f"晨报结构校验失败：{e}") from e
