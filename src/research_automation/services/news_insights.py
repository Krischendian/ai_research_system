"""
新闻智能聚类、重要性评分与分析师早评（单次 LLM + 24h 磁盘缓存）。

供晨报、昨日总结、隔夜速递共用；控制输入条数以节省 token。
"""
from __future__ import annotations

import hashlib
import json
import logging
import re
import time
from pathlib import Path
from typing import Any, Sequence

from research_automation.extractors.llm_client import chat
from research_automation.models.news import ClusterNewsItem, NewsCluster, NewsItem
from research_automation.models.news import OvernightNewsItem

logger = logging.getLogger(__name__)

# 送入聚类/评分模型的最大条数（超出则截断并打日志）
INSIGHT_MAX_INPUT_ITEMS = 50
# 单条 title/summary 截断长度，控制 prompt 体积
_INSIGHT_TITLE_MAX = 220
_INSIGHT_SUMMARY_MAX = 320
# 缓存目录与 TTL
_CACHE_TTL_SEC = 24 * 3600
_INSIGHTS_CACHE_DIR_NAME = "news_insights"


def _project_root() -> Path:
    return Path(__file__).resolve().parents[3]


def _cache_dir() -> Path:
    d = _project_root() / "data" / "raw" / _INSIGHTS_CACHE_DIR_NAME
    d.mkdir(parents=True, exist_ok=True)
    return d


def _stable_payload_hash(flat: list[dict[str, Any]]) -> str:
    """对输入简讯做稳定哈希，用于同日同批缓存键。"""
    minimal = [
        {
            "t": (x.get("title") or "")[:_INSIGHT_TITLE_MAX],
            "s": (x.get("summary") or "")[:_INSIGHT_SUMMARY_MAX],
            "u": x.get("source_url") or "",
            "src": x.get("source") or "",
        }
        for x in flat
    ]
    raw = json.dumps(minimal, ensure_ascii=False, sort_keys=True)
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()[:24]


def _cache_path(context: str, date_key: str, h: str) -> Path:
    safe_ctx = re.sub(r"[^\w\-]+", "_", context)[:40]
    return _cache_dir() / f"{safe_ctx}_{date_key}_{h}.json"


def _read_cache(path: Path) -> dict[str, Any] | None:
    if not path.exists() or path.stat().st_size == 0:
        return None
    age = time.time() - path.stat().st_mtime
    if age > _CACHE_TTL_SEC:
        return None
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None


def _write_cache(path: Path, payload: dict[str, Any]) -> None:
    try:
        path.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")
    except OSError as e:
        logger.warning("新闻洞察缓存写入失败 %s: %s", path, e)


def _extract_json_object(raw: str) -> dict[str, Any]:
    """从模型回复中提取 JSON 对象。"""
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


def _truncate_flat(flat: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """截断条数与字段长度。"""
    if len(flat) > INSIGHT_MAX_INPUT_ITEMS:
        logger.warning(
            "新闻洞察输入条数 %d 超过上限 %d，已截断",
            len(flat),
            INSIGHT_MAX_INPUT_ITEMS,
        )
        flat = flat[:INSIGHT_MAX_INPUT_ITEMS]
    out: list[dict[str, Any]] = []
    for x in flat:
        out.append(
            {
                "title": (x.get("title") or "")[:_INSIGHT_TITLE_MAX],
                "summary": (x.get("summary") or "")[:_INSIGHT_SUMMARY_MAX],
                "source_url": x.get("source_url"),
                "source": (x.get("source") or "")[:80],
                "published_at": x.get("published_at"),
                "matched_tickers": list(x.get("matched_tickers") or [])[:12],
                "sentiment": x.get("sentiment"),
            }
        )
    return out


def _build_insight_prompt(
    numbered: list[dict[str, Any]],
    monitor_tickers: list[str],
) -> str:
    """构造单次 LLM 提示：聚类 + 评分 + 早评。"""
    mt = ", ".join(monitor_tickers[:30]) if monitor_tickers else "（无）"
    body = json.dumps(numbered, ensure_ascii=False)
    return f"""你是投研助理。以下为按顺序编号的新闻简讯（JSON 数组，字段 i 为编号从 0 开始）。

监控池 ticker（命中可加分，未出现不扣分）：{mt}

请**一次性**完成：
1. **聚类**：标题/主题高度相似、同一事件多信源报道的编号归入同一聚类；每聚类给英文 snake_case 的 cluster_id、**中文** representative_title（概括合并后的核心信息）。
2. **重要性**：对每个聚类给 importance_score（1-10 整数）；并对每条原始编号给分 item_scores（可与所属聚类主分一致或微调）。
   - 9-10：地缘/央行/监管/大盘系统性风险等必读；
   - 7-8：重要行业或龙头公司实质影响；
   - 4-6：背景信息；
   - 1-3：重复炒作或低信息密度。
   涉及监控池标的、时效强（盘前盘后突发）可酌情加分。
3. **analyst_briefing**：**200 汉字以内**，投研视角一段话，**不要**罗列「第一条/第二条」或具体分数，只写逻辑与关注焦点。

输出**仅一个** JSON 对象，键必须为：
clusters, item_scores, analyst_briefing

clusters 元素形状：
{{"cluster_id":"string","representative_title":"中文","importance_score":7,"item_indices":[0,2]}}

item_scores 元素形状：{{"index":0,"score":8}}

要求：每个编号 0..{len(numbered)-1} 必须**恰好出现在某一个聚类的 item_indices 中一次**，不得遗漏或重复。

输入：
{body}
"""


def _ensure_cluster_coverage(
    clusters_raw: list[dict[str, Any]],
    n: int,
    flat: list[dict[str, Any]],
) -> list[dict[str, Any]]:
    """补全未被模型覆盖的编号为单条聚类，避免丢稿。"""
    covered: set[int] = set()
    out: list[dict[str, Any]] = []
    for c in clusters_raw:
        if not isinstance(c, dict):
            continue
        idxs: list[int] = []
        for x in c.get("item_indices") or []:
            try:
                i = int(x)
            except (TypeError, ValueError):
                continue
            if 0 <= i < n and i not in covered:
                idxs.append(i)
                covered.add(i)
        if not idxs:
            continue
        cid = str(c.get("cluster_id") or f"cluster_{idxs[0]}").strip() or f"cluster_{idxs[0]}"
        title = str(c.get("representative_title") or "").strip()
        if not title:
            title = flat[idxs[0]].get("title") or "未命名主题"
        try:
            sc = int(c.get("importance_score", 5))
        except (TypeError, ValueError):
            sc = 5
        sc = max(1, min(10, sc))
        out.append(
            {
                "cluster_id": cid[:64],
                "representative_title": title[:500],
                "importance_score": sc,
                "item_indices": sorted(idxs),
            }
        )
    for i in range(n):
        if i in covered:
            continue
        t = flat[i].get("title") or f"单条_{i}"
        out.append(
            {
                "cluster_id": f"singleton_{i}",
                "representative_title": str(t)[:200],
                "importance_score": 5,
                "item_indices": [i],
            }
        )
        covered.add(i)
    return out


def _expand_clusters(
    clusters_norm: list[dict[str, Any]],
    item_score_map: dict[int, int],
    flat: list[dict[str, Any]],
) -> list[NewsCluster]:
    """将聚类定义与原始简讯合并为 API 模型。"""
    result: list[NewsCluster] = []
    for c in clusters_norm:
        members: list[ClusterNewsItem] = []
        cluster_sc = int(c.get("importance_score", 5))
        cluster_sc = max(1, min(10, cluster_sc))
        for idx in c["item_indices"]:
            d = flat[idx]
            sc = item_score_map.get(idx, cluster_sc)
            sc = max(1, min(10, sc))
            tick = list(d.get("matched_tickers") or [])
            members.append(
                ClusterNewsItem(
                    title=d.get("title") or "",
                    summary=d.get("summary") or "",
                    source=d.get("source") or "",
                    source_url=d.get("source_url"),
                    published_at=d.get("published_at"),
                    matched_tickers=tick,
                    importance_score=sc,
                    sentiment=d.get("sentiment"),
                )
            )
        result.append(
            NewsCluster(
                cluster_id=c["cluster_id"],
                representative_title=c["representative_title"],
                importance_score=cluster_sc,
                news_items=members,
            )
        )
    return result


def _top_news_from_clusters(
    clusters: list[NewsCluster],
    *,
    min_score: int = 7,
    limit: int = 40,
) -> list[ClusterNewsItem]:
    """扁平化聚类，取高分条目，按分数降序。"""
    flat: list[ClusterNewsItem] = []
    for cl in clusters:
        for it in cl.news_items:
            if it.importance_score >= min_score:
                flat.append(it)
    flat.sort(key=lambda x: x.importance_score, reverse=True)
    return flat[:limit]


def apply_scores_to_morning_items(
    macro: list[NewsItem],
    company: list[NewsItem],
    scores: dict[int, int],
) -> tuple[list[NewsItem], list[NewsItem]]:
    """按合并顺序（宏观在前、公司在后）写回 ``importance_score``。"""
    nm: list[NewsItem] = []
    for i, it in enumerate(macro):
        sc = scores.get(i, 5)
        nm.append(it.model_copy(update={"importance_score": sc}))
    off = len(macro)
    nc: list[NewsItem] = []
    for j, it in enumerate(company):
        sc = scores.get(off + j, 5)
        nc.append(it.model_copy(update={"importance_score": sc}))
    return nm, nc


def news_items_to_flat_dicts(items: Sequence[NewsItem]) -> list[dict[str, Any]]:
    """将晨报 ``NewsItem`` 转为洞察输入行。"""
    out: list[dict[str, Any]] = []
    for it in items:
        out.append(
            {
                "title": it.title,
                "summary": it.summary,
                "source_url": it.source_url,
                "source": it.source,
                "published_at": it.published_at,
                "matched_tickers": list(it.matched_tickers),
                "sentiment": it.sentiment,
            }
        )
    return out


def overnight_items_to_flat_dicts(items: Sequence[OvernightNewsItem]) -> list[dict[str, Any]]:
    """将隔夜 ``OvernightNewsItem`` 转为洞察输入行。"""
    out: list[dict[str, Any]] = []
    for it in items:
        out.append(
            {
                "title": it.title,
                "summary": it.summary,
                "source_url": it.source_url,
                "source": it.source,
                "published_at": it.published_at_ny,
                "matched_tickers": list(it.matched_tickers),
            }
        )
    return out


def raw_articles_to_flat_dicts(articles: list[Any]) -> list[dict[str, Any]]:
    """
    将类 RawArticle 的字典列表转为洞察输入（仅 implied_tickers，不含全文关键词匹配）。
    """
    from datetime import datetime, timezone

    out: list[dict[str, Any]] = []
    for art in articles:
        if not isinstance(art, dict):
            continue
        title = (art.get("title") or "").strip()
        if not title:
            continue
        desc = (art.get("description") or "").strip()
        link = (art.get("link") or "").strip() or None
        src = str(art.get("source") or "")
        pub: str | None = None
        u = art.get("finnhub_datetime_unix")
        if u is not None:
            try:
                dt_utc = datetime.fromtimestamp(int(u), tz=timezone.utc)
                pub = dt_utc.replace(microsecond=0).isoformat().replace("+00:00", "Z")
            except (TypeError, ValueError, OSError):
                pass
        if not pub:
            p = art.get("published_at_utc")
            pub = str(p).strip() if p else None
        tick: list[str] = []
        for x in art.get("implied_tickers") or []:
            uu = str(x).strip().upper()
            if uu:
                tick.append(uu)
        out.append(
            {
                "title": title,
                "summary": desc[:800],
                "source_url": link,
                "source": src,
                "published_at": pub,
                "matched_tickers": sorted(set(tick)),
            }
        )
    return out


def raw_articles_with_tickers_to_flat(
    articles: list[Any],
    active_tickers: set[str],
) -> list[dict[str, Any]]:
    """
    将 RawArticle 列表转为洞察输入，并合并 ``extract_tickers_from_text`` 与 implied_tickers。
    """
    from datetime import datetime, timezone

    from research_automation.extractors.news_client import extract_tickers_from_text

    out: list[dict[str, Any]] = []
    for art in articles:
        if not isinstance(art, dict):
            continue
        title = (art.get("title") or "").strip()
        if not title:
            continue
        desc = (art.get("description") or "").strip()
        link = (art.get("link") or "").strip() or None
        src = str(art.get("source") or "")
        pub: str | None = None
        u = art.get("finnhub_datetime_unix")
        if u is not None:
            try:
                dt_utc = datetime.fromtimestamp(int(u), tz=timezone.utc)
                pub = dt_utc.replace(microsecond=0).isoformat().replace("+00:00", "Z")
            except (TypeError, ValueError, OSError):
                pass
        if not pub:
            p = art.get("published_at_utc")
            pub = str(p).strip() if p else None
        blob = f"{title} {desc}"
        tickers_set = set(extract_tickers_from_text(blob))
        for x in art.get("implied_tickers") or []:
            uu = str(x).strip().upper()
            if uu in active_tickers:
                tickers_set.add(uu)
        out.append(
            {
                "title": title[:_INSIGHT_TITLE_MAX],
                "summary": desc[:_INSIGHT_SUMMARY_MAX],
                "source_url": link,
                "source": src[:80],
                "published_at": pub,
                "matched_tickers": sorted(tickers_set),
            }
        )
    return out


def compute_news_insights(
    flat_items: list[dict[str, Any]],
    *,
    context: str,
    date_key: str,
    monitor_tickers: list[str],
) -> tuple[list[NewsCluster], list[ClusterNewsItem], str, dict[int, int]]:
    """
    对扁平简讯列表执行聚类、评分与分析师早评。

    :return: (clusters, top_news, analyst_briefing, index->score 映射)
    """
    if not flat_items:
        return [], [], "", {}

    flat = _truncate_flat(flat_items)
    n = len(flat)
    h = _stable_payload_hash(flat)
    cpath = _cache_path(context, date_key, h)
    cached = _read_cache(cpath)
    if cached and cached.get("payload_hash") == h:
        logger.info(
            "新闻洞察命中缓存 context=%s date=%s 条数=%d",
            context,
            date_key,
            n,
        )
        try:
            item_score_map: dict[int, int] = {}
            for k, v in (cached.get("item_score_map") or {}).items():
                try:
                    item_score_map[int(k)] = max(1, min(10, int(v)))
                except (TypeError, ValueError):
                    continue
            clusters_norm = cached.get("clusters_norm") or []
            clusters = _expand_clusters(clusters_norm, item_score_map, flat)
            top = _top_news_from_clusters(clusters)
            briefing = str(cached.get("analyst_briefing") or "")[:500]
            return clusters, top, briefing, item_score_map
        except (KeyError, TypeError, ValueError) as e:
            logger.warning("新闻洞察缓存损坏，将重算: %s", e)

    numbered = [
        {
            "i": i,
            "title": flat[i]["title"],
            "summary": flat[i]["summary"],
            "source": flat[i]["source"],
            "published_at": flat[i].get("published_at"),
            "tickers": flat[i].get("matched_tickers") or [],
        }
        for i in range(n)
    ]
    prompt = _build_insight_prompt(numbered, monitor_tickers)
    try:
        reply = chat(
            prompt,
            response_format={"type": "json_object"},
            timeout=120.0,
        )
    except (ValueError, RuntimeError) as e:
        logger.warning("新闻洞察 LLM 调用失败，返回空洞察: %s", e)
        return [], [], "", {}

    try:
        data = _extract_json_object(reply)
    except (json.JSONDecodeError, ValueError) as e:
        logger.warning("新闻洞察 JSON 解析失败: %s", e)
        return [], [], "", {}

    clusters_raw = data.get("clusters") if isinstance(data.get("clusters"), list) else []
    scores_raw = data.get("item_scores") if isinstance(data.get("item_scores"), list) else []
    briefing = str(data.get("analyst_briefing") or "").strip()[:220]

    item_score_map: dict[int, int] = {}
    for it in scores_raw:
        if not isinstance(it, dict):
            continue
        try:
            idx = int(it.get("index"))
            sc = int(it.get("score", 5))
        except (TypeError, ValueError):
            continue
        if 0 <= idx < n:
            item_score_map[idx] = max(1, min(10, sc))

    clusters_norm = _ensure_cluster_coverage(
        [x for x in clusters_raw if isinstance(x, dict)],
        n,
        flat,
    )
    clusters = _expand_clusters(clusters_norm, item_score_map, flat)
    top = _top_news_from_clusters(clusters)

    for idx in range(n):
        if idx not in item_score_map:
            for cl in clusters_norm:
                if idx in cl["item_indices"]:
                    try:
                        item_score_map[idx] = max(
                            1, min(10, int(cl.get("importance_score", 5)))
                        )
                    except (TypeError, ValueError):
                        item_score_map[idx] = 5
                    break
            else:
                item_score_map[idx] = 5

    _write_cache(
        cpath,
        {
            "payload_hash": h,
            "llm_result": data,
            "clusters_norm": clusters_norm,
            "item_score_map": {str(k): v for k, v in item_score_map.items()},
            "analyst_briefing": briefing,
        },
    )
    logger.info(
        "新闻洞察完成 context=%s date=%s 聚类数=%d top(>=7)=%d",
        context,
        date_key,
        len(clusters),
        len(top),
    )
    return clusters, top, briefing, item_score_map
