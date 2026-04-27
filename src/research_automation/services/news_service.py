"""晨报：宏观为多源 RSS（按序回退 + 缓存），仍空则回退 RSS_FEEDS；公司优先 Benzinga，失败或为空时回退 Finnhub。"""
from __future__ import annotations

import json
import re
from datetime import date, datetime, timedelta, timezone
from typing import Any

from zoneinfo import ZoneInfo

from research_automation.core.company_manager import get_active_tickers
from research_automation.extractors.finnhub_news import (
    company_news_item_to_raw_article,
    get_company_news as finnhub_get_company_news,
    merge_finnhub_and_rss,
)
from research_automation.extractors.llm_client import chat
from research_automation.extractors.benzinga_client import (
    benzinga_news_dict_to_raw_article,
    get_company_news as benzinga_get_company_news,
)
from research_automation.extractors.news_client import (
    RawArticle,
    extract_tickers_from_text,
    fetch_macro_news_with_fallback,
    fetch_rss_articles,
)
from research_automation.models.news import MorningBrief, NewsItem
from research_automation.services.news_insights import (
    apply_scores_to_morning_items,
    compute_news_insights,
    news_items_to_flat_dicts,
)


class NewsBriefError(Exception):
    """晨报生成失败（可映射为 HTTP 说明）。"""

    def __init__(self, message: str) -> None:
        self.message = message
        super().__init__(message)


def _extract_json_object(raw: str) -> dict[str, Any]:
    """从模型回复中解析 JSON 对象。"""
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


def _normalize_news_date(d: date | str) -> str:
    if isinstance(d, date):
        return d.isoformat()
    s = str(d).strip()
    return s[:10] if len(s) >= 10 else s


def _is_finnhub_article(a: RawArticle) -> bool:
    """判断是否来自结构化公司新闻源（Benzinga 或 Finnhub；合并后 source 以对应前缀开头）。"""
    s = str(a.get("source") or "").strip()
    return s.startswith("Finnhub") or s.startswith("Benzinga")


def _published_at_from_article(a: RawArticle) -> str | None:
    """优先 Finnhub Unix，否则 ``published_at_utc``（ISO）。"""
    u = a.get("finnhub_datetime_unix")
    if u is not None:
        try:
            dt_utc = datetime.fromtimestamp(int(u), tz=timezone.utc)
            return dt_utc.replace(microsecond=0).isoformat().replace("+00:00", "Z")
        except (TypeError, ValueError, OSError):
            pass
    iso = a.get("published_at_utc")
    if iso:
        s = str(iso).strip()
        return s or None
    return None


def _build_split_prompt(macro: list[RawArticle], company: list[RawArticle]) -> str:
    """分别编号宏观（多源市场 RSS）与公司（Benzinga/Finnhub）素材，供模型严格分区归类。"""
    lines: list[str] = []
    lines.append("【宏观 — 路透 / 彭博 / WSJ / FT / Yahoo 等 RSS】仅以下编号可用于 macro_news：")
    for i, a in enumerate(macro, start=1):
        desc = (a.get("description") or "")[:800]
        lines.append(
            f"  M{i}. [来源:{a.get('source','')}] 标题:{a.get('title','')} 提要:{desc}"
        )
    lines.append("")
    lines.append("【公司 — Benzinga / Finnhub】仅以下编号可用于 company_news：")
    for i, a in enumerate(company, start=1):
        desc = (a.get("description") or "")[:800]
        lines.append(
            f"  C{i}. [来源:{a.get('source','')}] 标题:{a.get('title','')} 提要:{desc}"
        )
    return "\n".join(lines)


def _text_for_ticker_match(item: NewsItem, articles: list[RawArticle]) -> str:
    """合并标题、摘要及匹配到的原文提要，供 ticker 关键词命中。"""
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
            for x in a.get("implied_tickers") or []:
                parts.append(f"${str(x).strip().upper()}")
            break
    return " ".join(parts)


def _match_article_for_title(title: str, articles: list[RawArticle]) -> RawArticle | None:
    """按标题将 LLM 输出与原始条目关联（精确优先，再子串）。"""
    t = title.strip().lower()
    if not t:
        return None
    for a in articles:
        if a["title"].strip().lower() == t:
            return a
    for a in articles:
        at = a["title"].strip().lower()
        if t in at or at in t:
            return a
    return None


def _finalize_news_items(
    items: list[NewsItem],
    articles: list[RawArticle],
) -> list[NewsItem]:
    """补齐 source_url、published_at、matched_tickers。"""
    from research_automation.core.company_manager import list_companies

    active_pool = {
        c.ticker.strip().upper()
        for c in list_companies(active_only=True)
        if c.ticker.strip()
    }
    out: list[NewsItem] = []
    for it in items:
        blob = _text_for_ticker_match(it, articles)
        tickers = extract_tickers_from_text(blob)
        art = _match_article_for_title(it.title, articles)
        url = None
        pub: str | None = None
        if art is not None:
            link = (art.get("link") or "").strip()
            url = link or None
            pub = _published_at_from_article(art)
            for x in art.get("implied_tickers") or []:
                u = str(x).strip().upper()
                if u in active_pool:
                    tickers = sorted(set(tickers) | {u})
        out.append(
            it.model_copy(
                update={
                    "matched_tickers": tickers,
                    "source_url": url,
                    "published_at": pub,
                }
            )
        )
    return out


def get_company_news(
    ticker: str,
    from_date: date | str,
    to_date: date | str,
) -> list[RawArticle]:
    """
    公司新闻：优先 Benzinga ``/api/v2/news``；失败或空列表时回退 Finnhub ``company-news``。

    每条 ``RawArticle`` 与画像/展示常用字段对应关系：

    - **title** → 标题
    - **description** → 摘要（与 Benzinga ``summary`` / Finnhub 摘要一致，可用作 ``summary``）
    - **link** → 原文链接（可用作 ``source_url``）
    - **published_at_utc**（及 ``finnhub_datetime_unix``）→ 发布时间；业务侧可用
      :func:`raw_article_to_profile_news_fields` 统一为 ``published_at`` 字符串
    """
    sym = (ticker or "").strip().upper()
    if not sym:
        return []

    fd = _normalize_news_date(from_date)
    td = _normalize_news_date(to_date)
    bz = benzinga_get_company_news(sym, fd, td)
    if bz:
        return [benzinga_news_dict_to_raw_article(it, sym) for it in bz]

    out: list[RawArticle] = []
    for it in finnhub_get_company_news(sym, from_date, to_date):
        out.append(company_news_item_to_raw_article(it, sym))
    return out


def raw_article_to_profile_news_fields(article: RawArticle) -> dict[str, str] | None:
    """
    将 ``RawArticle`` 转为画像补充逻辑使用的规范字段。

    无有效 ``link`` 时返回 ``None``（不生成无外链的新闻动态）。
    """
    url = (article.get("link") or "").strip()
    if not url:
        return None
    title = (article.get("title") or "").strip()
    summary = (article.get("description") or "").strip()
    pub = (article.get("published_at_utc") or "").strip()
    if not pub and "finnhub_datetime_unix" in article:
        try:
            ts = int(article["finnhub_datetime_unix"])
            pub = datetime.fromtimestamp(ts, tz=timezone.utc).isoformat().replace(
                "+00:00", "Z"
            )
        except (TypeError, ValueError, OSError):
            pub = ""
    return {
        "title": title,
        "summary": summary,
        "source_url": url,
        "published_at": pub,
    }


def fetch_company_news_raw_articles_for_tickers(
    tickers: list[str],
    from_date: date | str,
    to_date: date | str,
) -> list[RawArticle]:
    """多 ticker 拉取公司新闻（内部逐 symbol；Benzinga 优先、Finnhub 兜底）。"""
    merged: list[RawArticle] = []
    for sym in tickers:
        sym = sym.strip().upper()
        if not sym:
            continue
        merged.extend(get_company_news(sym, from_date, to_date))
    return merged


def _prioritize_articles_with_tickers(articles: list[RawArticle]) -> list[RawArticle]:
    """标题/提要已命中监控池的公司源条目排在前面。"""
    hits: list[RawArticle] = []
    rest: list[RawArticle] = []
    for a in articles:
        blob = f"{a.get('title') or ''} {a.get('description') or ''}"
        if extract_tickers_from_text(blob):
            hits.append(a)
        else:
            rest.append(a)
    return hits + rest


def get_morning_brief(
    *,
    max_rss_items: int = 32,
) -> MorningBrief:
    """
    宏观新闻使用 ``fetch_macro_news_with_fallback``（路透 / 彭博 / WSJ / FT / Yahoo 按序、10s 超时、6h 缓存）；
    若仍为空则回退 ``RSS_FEEDS`` 多源合并。公司新闻优先 Benzinga，失败或为空则回退 Finnhub。

    宏观与公司列表再经 ``merge_finnhub_and_rss`` 去重合并；不用 RSS 作为公司新闻兜底。
    """
    ny = ZoneInfo("America/New_York")
    today_ny = datetime.now(ny).date()
    from_ny = today_ny - timedelta(days=2)
    macro_feed, macro_from_cache = fetch_macro_news_with_fallback(
        max_items=max_rss_items,
    )
    if not macro_feed:
        macro_feed = fetch_rss_articles(
            max_items=max_rss_items,
            per_feed_limit=7,
        )
        macro_from_cache = False

    company_raw = fetch_company_news_raw_articles_for_tickers(
        get_active_tickers(),
        from_ny.isoformat(),
        today_ny.isoformat(),
    )
    if not macro_feed and not company_raw:
        raise NewsBriefError(
            "未能从宏观与公司新闻源获取任何新闻（网络或 RSS 不可用；公司侧请检查"
            " BENZINGA_API_KEY / FINNHUB_API_KEY）。请稍后重试。"
        )

    merged = merge_finnhub_and_rss(company_raw, macro_feed)
    macro_articles = [a for a in merged if not _is_finnhub_article(a)]
    company_articles = [a for a in merged if _is_finnhub_article(a)]
    macro_articles = macro_articles[:25]
    company_articles = company_articles[:25]
    company_articles = _prioritize_articles_with_tickers(company_articles)

    body = _build_split_prompt(macro_articles, company_articles)
    prompt = f"""你是财经新闻编辑助手。下方分为两段素材：
- 【宏观 — 路透 / 彭博 / WSJ / FT / Yahoo 等 RSS】条目编号以 M 开头，**macro_news 只能从这一段选**，且 title 必须与对应条目的英文标题**完全一致**。
- 【公司 — Benzinga / Finnhub】条目编号以 C 开头，**company_news 只能从这一段选**，且 title 必须与对应条目的英文标题**完全一致**。

任务：
1. 为每条单独生成**中文摘要**（60～120 字），只综合已给出的标题与提要，禁止编造数字、机构立场或未出现的信息。
2. macro_news 仅覆盖 M 编号素材；company_news 仅覆盖 C 编号素材。不要跨区归类。
3. 每条输出字段：title（**原文英文标题**，与输入一致）、summary（中文摘要）、source（与输入来源字段**完全一致**）、sentiment（必填，对**该股/该主题短期市场影响**的粗分类，仅三选一：**positive** | **negative** | **neutral**；信息不足或纯事实无倾向时用 neutral）。
4. 只输出一个 JSON 对象，键为 macro_news 与 company_news，值均为数组，元素形状：{{"title","summary","source","sentiment"}}。
5. 若某一侧无可用条目，对应数组可为空。

素材：
{body}
"""

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
                out.append(
                    NewsItem(
                        title=t,
                        summary=s,
                        source=src,
                        sentiment=it.get("sentiment"),
                    )
                )
            return out

        macro = _finalize_news_items(_as_items(macro_raw), macro_articles)
        company_in = _finalize_news_items(_as_items(company_raw), company_articles)
        company = [it for it in company_in if it.matched_tickers]

        if not macro and not company:
            raise ValueError("模型未返回有效新闻条目")

        # 单次 LLM：聚类 + 重要性 + 分析师早评（结果 24h 磁盘缓存）
        flat_for_insights = news_items_to_flat_dicts(macro + company)
        clusters, top_news, analyst_briefing, score_map = compute_news_insights(
            flat_for_insights,
            context="morning_brief",
            date_key=today_ny.isoformat(),
            monitor_tickers=get_active_tickers(),
        )
        macro_scored, company_scored = apply_scores_to_morning_items(
            macro, company, score_map
        )

        macro_src_bits = [
            "宏观：多源 **市场 RSS** 按序回退（路透 Markets → 彭博 Markets → WSJ Markets →"
            " FT Markets → Yahoo News RSS；单次请求超时 10s），"
            "成功结果写入 ``data/cache/macro_news_cache.json``（**6 小时**有效）；"
            "全部失败时用未过期缓存。"
        ]
        if macro_from_cache:
            macro_src_bits.append("**本次宏观素材来自磁盘缓存。**")
        macro_src_bits.append(
            "若宏观链路与缓存皆空，再回退站内 **RSS_FEEDS**（Bloomberg / TechCrunch / Reuters 等）。"
            " 公司：优先 **Benzinga**（BENZINGA_API_KEY，24h 缓存），"
            "失败或为空时 **Finnhub**（FINNHUB_API_KEY）；与公司新闻合并去重后分区摘要。"
            " OpenAI 中文摘要与分类；聚类/评分/早评为第二次模型调用（见 clusters / top_news）。"
        )

        return MorningBrief(
            macro_news=macro_scored,
            company_news=company_scored,
            data_source_label=" ".join(macro_src_bits),
            provenance_note=(
                "摘要由模型基于提要生成，可能与报道原文不一致；"
                "请务必点击原文链接核对，不构成任何投资建议。"
            ),
            clusters=clusters,
            top_news=top_news,
            analyst_briefing=analyst_briefing,
        )
    except Exception as e:
        raise NewsBriefError(f"晨报结构校验失败：{e}") from e
