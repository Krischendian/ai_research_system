"""隔夜速递：新闻时区下隔夜窗口内筛选 + 一句中文要点（LLM）；时间以 Finnhub Unix 优先。"""
from __future__ import annotations

import json
import re
from datetime import datetime
from typing import Any
from zoneinfo import ZoneInfo

from research_automation.core.news_time import (
    article_published_in_news_tz,
    filter_articles_in_half_open_window,
    get_news_timezone_name,
    overnight_window,
)
from research_automation.core.company_manager import get_active_tickers
from research_automation.core.sector_config import get_sector_macro_keywords
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
from research_automation.services.news_postprocess import post_process_payload
from research_automation.services.news_service import (
    NewsBriefError,
    fetch_company_news_raw_articles_for_tickers,
)

_NY = ZoneInfo("America/New_York")


def _escape_unescaped_quotes_in_json_strings(text: str) -> str:
    """修复 JSON 字符串中的裸双引号，避免破坏语法。"""
    out: list[str] = []
    in_string = False
    escaped = False
    n = len(text)
    i = 0
    while i < n:
        ch = text[i]
        if not in_string:
            out.append(ch)
            if ch == '"':
                in_string = True
                escaped = False
            i += 1
            continue

        if escaped:
            out.append(ch)
            escaped = False
            i += 1
            continue

        if ch == "\\":
            out.append(ch)
            escaped = True
            i += 1
            continue

        if ch == '"':
            j = i + 1
            while j < n and text[j] in " \t\r\n":
                j += 1
            if j >= n or text[j] in ",}:]":
                out.append('"')
                in_string = False
            else:
                out.append('\\"')
            i += 1
            continue

        out.append(ch)
        i += 1
    return "".join(out)


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
        # 尝试修复：先处理全角引号，再修复字符串中的裸 ASCII 双引号。
        fixed = s.replace("\u201c", '\\"').replace("\u201d", '\\"')
        fixed = _escape_unescaped_quotes_in_json_strings(fixed)
        try:
            obj = json.loads(fixed)
            return obj if isinstance(obj, dict) else {}
        except Exception:
            pass
    m = re.search(r"\{[\s\S]*\}", s)
    if m:
        try:
            inner = m.group(0)
            inner = inner.replace("\u201c", '\\"').replace("\u201d", '\\"')
            inner = _escape_unescaped_quotes_in_json_strings(inner)
            obj = json.loads(inner)
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
    sector: str | None = None,
) -> str:
    from research_automation.core.sector_config import (
        get_sector_watch_items_str,
        get_sector_macro_keywords,
    )
    tz_label = get_news_timezone_name()
    window = f"{start.strftime('%Y-%m-%d %H:%M')} 至 {end.strftime('%Y-%m-%d %H:%M')} ({tz_label})"
    tickers_str = ", ".join(sorted(active_tickers)[:50])
    watch_items = get_sector_watch_items_str(sector) if sector else "- 所有重要公司事件"

    macro_lines = []
    for i, a in enumerate(macro_articles, 1):
        url = (a.get("link") or "").strip()
        url_part = f"[URL:{url}]" if url else "[URL:无]"
        macro_lines.append(
            f"M{i}. [来源:{a.get('source','')}]{url_part} "
            f"{a.get('title','')} | {(a.get('description') or '')[:400]}"
        )

    company_lines = []
    for i, a in enumerate(company_articles, 1):
        tickers = a.get("implied_tickers") or []
        url = (a.get("link") or "").strip()
        url_part = f"[URL:{url}]" if url else "[URL:无]"
        company_lines.append(
            f"C{i}. [Ticker:{','.join(tickers)}][来源:{a.get('source','')}]{url_part} "
            f"{a.get('title','')} | {(a.get('description') or '')[:400]}"
        )

    return f"""你是专业财经编辑，为机构分析师撰写隔夜简报。
时间窗口：{window}
板块：{sector or '全市场'}
监控Ticker池：{tickers_str}

【宏观新闻素材】（M编号）：
{chr(10).join(macro_lines) or '（无）'}

【公司新闻素材】（C编号）：
{chr(10).join(company_lines) or '（无）'}

【本板块公司新闻关注点】：
{watch_items}

=== 任务说明 ===

一、宏观新闻处理规则：
选出真正重要的全球宏观新闻，必须是以下类型之一：
- 地缘政治重大事件
- 央行决策或官员发言
- 国家领导人发言或重大政策
- 重要经济数据发布
重点地区：北美、欧洲、中东、亚洲（中国/日本/韩国/印度）

过滤掉：
- 纯价格涨跌播报（无实质原因的市场价格描述）
- 无实质内容的会议预告
- importance_score ≤ 4 的条目
- 重复角度的同一事件（同一事件最多1条）

每条宏观新闻输出字段：
- title：原标题（不得翻译或改写）
- summary：中文摘要50-100字，只用原文信息
- region：North America / Europe / Middle East / Asia / Global 五选一
- source：来源
- source_url：从[URL:...]原样复制，[URL:无]则输出""
- importance_score：1-10整数

二、公司新闻处理规则：
只选监控Ticker池内的公司，且新闻必须属于以下类型之一：
- 新研究/分析师评级变动
- 新合作或重要合同
- 收购并购（M&A）
- 财务业绩或指引
- 股票回购
- Insider买卖
- 管理层重要发言

过滤掉：
- 纯股价涨跌播报（无实质原因）
- 第三方品牌进驻/供应商公告（非目标公司自身事件）
- 与【本板块公司新闻关注点】完全无关的泛论
- importance_score ≤ 4 的条目
- 同一公司同一事件最多输出1条最具信息量的

每条公司新闻输出字段：
- ticker：大写ticker
- title：原标题（不得翻译或改写）
- summary：中文摘要50-100字，只用原文信息
- event_type：earnings / partnership / ma / buyback / insider_trade / management / research / other 八选一
- source：来源
- source_url：从[URL:...]原样复制
- importance_score：1-10整数

三、汇总输出：
1. macro_today_theme：1句话点出今日宏观最核心主线，例如"美联储维持利率不变，伊朗局势推动油价破126美元"
2. macro_summary：按地区分组的中文叙事段落200-300字，格式：【北美】...【欧洲】...【中东】...【亚洲】...，无内容的地区省略
3. company_summary：按重要性排序的公司动态叙事段落150-200字，点明每家有动态公司的核心事件
4. no_news_tickers：监控Ticker池中本期无任何实质动态的ticker列表
5. overnight_summary：以"隔夜重点关注："开头的一句话总结

【输出约束 — 必须严格遵守】
1. source_url 必须从对应M/C行的[URL:...]原样复制，禁止修改或自行构造
2. summary 只能使用输入原文已有信息，禁止添加背景知识或推断
3. 若原文信息不足，输出："原文信息不足，仅见标题"
4. title 必须与输入原标题完全一致，不得翻译或改写
5. 同一ticker同一事件最多输出1条
6. 宏观优先Bloomberg来源；公司新闻优先Benzinga来源，Bloomberg次之，Finnhub-Yahoo等转载最后考虑
7. importance_score ≤ 5 的条目禁止输出。
   summary 内容为"原文信息不足，仅见标题"的条目直接忽略，不输出。
8. 纯价格播报、供应商进驻公告、与关注点无关的泛论直接忽略

只输出JSON，格式：
{{
  "macro_today_theme": "一句话主线",
  "macro_summary": "【北美】...【欧洲】...【中东】...【亚洲】...",
  "macro_news": [{{"title":"...","summary":"...","region":"...","source":"...","source_url":"...","importance_score":8}}],
  "company_summary": "各公司动态叙事段落",
  "company_news": [{{"ticker":"AAPL","title":"...","summary":"...","event_type":"earnings","source":"...","source_url":"...","importance_score":7}}],
  "no_news_tickers": ["ZM","DG"],
  "overnight_summary": "隔夜重点关注：..."
}}

不要输出任何JSON以外的内容。"""


def get_overnight_news(
    *,
    sector: str | None = None,
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
        get_active_tickers(sector=sector),
        from_d.isoformat(),
        to_d.isoformat(),
    )
    articles = merge_finnhub_and_rss(company_raw, rss_batch)
    if not articles:
        raise NewsBriefError(
            "未能从 RSS 与公司新闻源获取任何新闻（网络、密钥或 feed 不可用）。请稍后重试。"
        )

    filtered = filter_articles_in_half_open_window(articles, start, end)
    active = set(get_active_tickers(sector=sector))

    macro_keywords = get_sector_macro_keywords(sector) if sector else []

    def _macro_keyword_match(a: RawArticle) -> bool:
        if not macro_keywords:
            return True
        text = f"{a.get('title', '')} {a.get('description', '')}".lower()
        return any(kw.lower() in text for kw in macro_keywords)

    def _is_company_article(a: RawArticle) -> bool:
        implied = [str(x).strip().upper() for x in (a.get("implied_tickers") or [])]
        if any(t in active for t in implied):
            return True
        txt = f"{a.get('title') or ''} {a.get('description') or ''}"
        guessed = extract_tickers_from_text(txt)
        return any(t in active for t in guessed)

    macro_articles = [
        a for a in filtered if not _is_company_article(a) and _macro_keyword_match(a)
    ]
    company_articles = [a for a in filtered if _is_company_article(a)]

    if not filtered:
        return OvernightNewsResponse(
            overnight_summary=(
                "隔夜重点关注：本时间窗内未匹配到带有效发布时间的新闻条目；"
                "可能因网络延迟或各源未暴露时间戳，请稍后重试或结合下方完整晨报浏览。"
            ),
            macro_today_theme="",
            macro_news=[],
            company_news=[],
            no_news_tickers=[],
            window_start_ny=start.isoformat(),
            window_end_ny=end.isoformat(),
            analyst_briefing="",
            provenance_note="",
        )

    prompt = _build_overnight_prompt(
        macro_articles, company_articles, active, start, end, sector=sector
    )
    url_map: dict[str, str] = {}
    for i, a in enumerate(macro_articles, 1):
        url = (a.get("link") or "").strip()
        if url:
            url_map[f"M{i}"] = url
    for i, a in enumerate(company_articles, 1):
        url = (a.get("link") or "").strip()
        if url:
            url_map[f"C{i}"] = url

    try:
        reply = chat(
            prompt,
            response_format={"type": "json_object"},
            timeout=120.0,
            max_tokens=16000,
        )
    except ValueError as e:
        raise NewsBriefError(f"语言模型未就绪：{e}") from e
    except RuntimeError as e:
        raise NewsBriefError(f"调用语言模型失败：{e}") from e

    payload = _safe_json(reply)
    payload = post_process_payload(payload, active)
    title_to_url: dict[str, str] = {}
    for i, a in enumerate(macro_articles, 1):
        url = (a.get("link") or "").strip()
        if url:
            title_to_url[a.get("title", "").strip()] = url
    for i, a in enumerate(company_articles, 1):
        url = (a.get("link") or "").strip()
        if url:
            title_to_url[a.get("title", "").strip()] = url
    title_to_pub_ny: dict[str, str] = {}
    for a in macro_articles + company_articles:
        title = (a.get("title") or "").strip()
        pub_utc = (a.get("published_at_utc") or "").strip()
        if title and pub_utc:
            try:
                if pub_utc.endswith("Z"):
                    pub_utc = pub_utc[:-1] + "+00:00"
                dt = datetime.fromisoformat(pub_utc).astimezone(_NY)
                title_to_pub_ny[title] = dt.isoformat()
            except (ValueError, TypeError):
                pass
    for item in payload.get("macro_news", []):
        if isinstance(item, dict):
            t = str(item.get("title", "")).strip()
            if not item.get("source_url"):
                item["source_url"] = title_to_url.get(t, "")
            if not item.get("published_at_ny"):
                item["published_at_ny"] = title_to_pub_ny.get(t, None)
    for item in payload.get("company_news", []):
        if isinstance(item, dict):
            t = str(item.get("title", "")).strip()
            if not item.get("source_url"):
                item["source_url"] = title_to_url.get(t, "")
            if not item.get("published_at_ny"):
                item["published_at_ny"] = title_to_pub_ny.get(t, None)
    macro_today_theme = str(payload.get("macro_today_theme") or "").strip()
    no_news_tickers = payload.get("no_news_tickers", [])
    if not isinstance(no_news_tickers, list):
        no_news_tickers = []
    no_news_tickers = [str(t).strip().upper() for t in no_news_tickers if str(t).strip()]
    macro_summary = str(payload.get("macro_summary") or "").strip()
    company_summary = str(payload.get("company_summary") or "").strip()
    macro_news = [MacroNewsItem(**item) for item in payload.get("macro_news", [])]
    company_news = [CompanyNewsItem(**item) for item in payload.get("company_news", [])]
    overnight_summary = _normalize_overnight_summary(payload.get("overnight_summary", "隔夜重点关注：无数据"))

    return OvernightNewsResponse(
        overnight_summary=overnight_summary,
        macro_today_theme=macro_today_theme,
        macro_summary=macro_summary,
        company_summary=company_summary,
        macro_news=macro_news,
        company_news=company_news,
        no_news_tickers=no_news_tickers,
        window_start_ny=start.isoformat(),
        window_end_ny=end.isoformat(),
        analyst_briefing="",
        provenance_note="",
    )
