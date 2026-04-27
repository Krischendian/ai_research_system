"""全局关键词搜索与基于片段的简答（RAG）。"""
from __future__ import annotations

import json
import os
import re
from pathlib import Path
from typing import Any

from dotenv import load_dotenv

_ROOT = Path(__file__).resolve().parents[3]
_STOPWORDS_EN = frozenset(
    "a an the is are was were be been being to of in on for with as at by "
    "or from it its this that these those what which who how when where why".split()
)


def _project_root() -> Path:
    return _ROOT


def _snippet_around(haystack: str, needle: str, radius: int = 220) -> str:
    if not haystack or not needle:
        return ""
    lo = haystack.lower()
    n = needle.lower()
    i = lo.find(n)
    if i < 0:
        return ""
    start = max(0, i - radius)
    end = min(len(haystack), i + len(needle) + radius)
    chunk = haystack[start:end]
    chunk = re.sub(r"\s+", " ", chunk).strip()
    if start > 0:
        chunk = "…" + chunk
    if end < len(haystack):
        chunk = chunk + "…"
    return chunk


def _match_in_text(text: str, query: str) -> bool:
    if not text or not query:
        return False
    return query.lower() in text.lower()


def _search_10k_files(query: str, limit: int, out: list[dict[str, Any]]) -> None:
    root = _project_root() / "data" / "raw" / "10k"
    if not root.is_dir():
        return
    paths = sorted(root.glob("*.txt"), key=lambda p: p.name)
    for path in paths:
        if len(out) >= limit:
            return
        if "_full." in path.name or path.name.endswith(".html"):
            continue
        try:
            body = path.read_text(encoding="utf-8", errors="replace")
        except OSError:
            continue
        if not _match_in_text(body, query):
            continue
        m = re.match(
            r"^([A-Z0-9._-]+)_(\d{4})_sec_([a-z0-9_]+)\.txt$",
            path.name,
            re.I,
        )
        sym = m.group(1).upper() if m else path.stem[:8]
        year = m.group(2) if m else "?"
        section = (m.group(3) if m else "section").replace("_", " ")
        snip = _snippet_around(body, query.strip() or query)
        if not snip:
            continue
        out.append(
            {
                "title": f"10-K {sym} ({year}) · {section}",
                "snippet": snip,
                "source_url": f"https://www.sec.gov/edgar/search/#/q={sym}",
            }
        )


def _iter_news_like_items(obj: Any) -> list[dict[str, Any]]:
    """从晨报/隔夜类 JSON 结构中尽量抽出扁平新闻 dict 列表。"""
    found: list[dict[str, Any]] = []
    if not isinstance(obj, dict):
        return found
    for key in ("macro_news", "company_news", "top_news"):
        arr = obj.get(key)
        if isinstance(arr, list):
            for it in arr:
                if isinstance(it, dict):
                    found.append(it)
    clusters = obj.get("clusters")
    if isinstance(clusters, list):
        for cl in clusters:
            if not isinstance(cl, dict):
                continue
            items = cl.get("items") or cl.get("news_items")
            if isinstance(items, list):
                for it in items:
                    if isinstance(it, dict):
                        found.append(it)
    return found


def _search_morning_brief_json(query: str, limit: int, out: list[dict[str, Any]]) -> None:
    reports = _project_root() / "data" / "reports"
    if not reports.is_dir():
        return
    candidates: list[Path] = []
    for pat in ("morning_brief*.json", "*.json"):
        candidates.extend(reports.glob(pat))
    if not candidates:
        return
    candidates.sort(key=lambda p: p.stat().st_mtime, reverse=True)
    for path in candidates[:5]:
        if len(out) >= limit:
            return
        try:
            raw = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError, TypeError):
            continue
        if not isinstance(raw, dict):
            continue
        items = _iter_news_like_items(raw)
        for it in items:
            if len(out) >= limit:
                return
            title = str(it.get("title") or "").strip()
            summary = str(it.get("summary") or it.get("description") or "").strip()
            blob = f"{title}\n{summary}"
            if not _match_in_text(blob, query):
                continue
            url = it.get("source_url") or it.get("link") or it.get("url")
            out.append(
                {
                    "title": f"News: {title}" if title else f"News: {path.name}",
                    "snippet": _snippet_around(blob, query) or summary[:400],
                    "source_url": str(url).strip() if url else None,
                }
            )


def _search_earnings_transcript_files(query: str, limit: int, out: list[dict[str, Any]]) -> None:
    root = _project_root() / "data" / "raw" / "earnings_transcripts"
    if not root.is_dir():
        return
    for path in sorted(root.rglob("*.txt"), key=lambda p: str(p)):
        if len(out) >= limit:
            return
        try:
            body = path.read_text(encoding="utf-8", errors="replace")
        except OSError:
            continue
        if not _match_in_text(body, query):
            continue
        name = path.stem
        m = re.search(r"([A-Z]{1,5})\D*(\d{4}Q[1-4])", name, re.I)
        sym = (m.group(1) if m else "UNK").upper()
        quarter = (m.group(2) if m else "unknown").upper()
        snip = _snippet_around(body, query.strip() or query)
        if not snip:
            continue
        out.append(
            {
                "title": f"Earnings Call {quarter} · {sym}",
                "snippet": snip,
                "source_url": None,
            }
        )


def keyword_search(query: str, *, limit: int = 20) -> list[dict[str, Any]]:
    """
    子串检索（大小写不敏感）：``data/raw/10k/*.txt``、``data/reports`` 下晨报类 JSON、
    ``data/raw/earnings_transcripts/**/*.txt``（若存在）。
    """
    q = (query or "").strip()
    if not q:
        return []
    lim = max(1, min(200, int(limit)))
    out: list[dict[str, Any]] = []
    _search_10k_files(q, lim, out)
    if len(out) < lim:
        _search_morning_brief_json(q, lim, out)
    if len(out) < lim:
        _search_earnings_transcript_files(q, lim, out)
    return out[:lim]


def _expand_search_queries(question: str) -> list[str]:
    """主问题 + 简单英文分词（去停用词），用于多轮 keyword_search。"""
    q = (question or "").strip()
    if not q:
        return []
    parts = [q]
    for w in re.findall(r"[A-Za-z]{2,}", q):
        wl = w.lower()
        if wl not in _STOPWORDS_EN and len(wl) > 2:
            parts.append(w)
    # 去重保序
    seen: set[str] = set()
    uniq: list[str] = []
    for p in parts:
        k = p.lower()
        if k not in seen:
            seen.add(k)
            uniq.append(p)
    return uniq[:12]


def answer_question(question: str) -> dict[str, Any]:
    """
    基于 ``keyword_search`` 命中的片段，调用 OpenAI ``gpt-4o-mini`` 生成简短中文回答。

    须配置 ``OPENAI_API_KEY``（与 ``llm_client`` 的 Anthropic 优先策略独立，避免走 Claude）。
    """
    load_dotenv(_project_root() / ".env", override=False)
    q = (question or "").strip()
    if not q:
        return {
            "answer": "请输入具体问题或关键词。",
            "sources": [],
        }

    rows: list[dict[str, Any]] = []
    seen_key: set[str] = set()
    for sub in _expand_search_queries(q):
        for r in keyword_search(sub, limit=25):
            key = f"{r.get('title')}|{r.get('snippet', '')[:80]}"
            if key in seen_key:
                continue
            seen_key.add(key)
            rows.append(r)
        if len(rows) >= 28:
            break

    if not rows:
        return {
            "answer": (
                "未在本地缓存（10-K 节选、晨报/报告 JSON、电话会逐字稿目录）中检索到相关片段。"
                "可缩短或改用英文关键词，或先拉取 10-K / 运行晨报任务以生成可检索文件。"
            ),
            "sources": [],
        }

    top = rows[:12]
    context_blocks: list[str] = []
    sources: list[dict[str, Any]] = []
    for i, r in enumerate(top, start=1):
        title = str(r.get("title") or f"片段{i}").strip()
        snip = str(r.get("snippet") or "").strip()
        url = r.get("source_url")
        context_blocks.append(f"[{i}] {title}\n{snip}")
        sources.append(
            {
                "label": f"[{i}]",
                "title": title,
                "summary": snip[:600],
                "url": str(url).strip() if url else None,
            }
        )

    api_key = (os.getenv("OPENAI_API_KEY") or "").strip()
    if not api_key or api_key.lower() in ("your-api-key-here", "sk-placeholder"):
        raise ValueError("搜索问答需要有效的 OPENAI_API_KEY（gpt-4o-mini）。")

    try:
        from openai import APIError, APITimeoutError, AuthenticationError, OpenAI, RateLimitError
    except ImportError as e:
        raise RuntimeError("缺少 openai 库，请运行：pip install openai") from e

    model = (os.getenv("SEARCH_ASK_MODEL") or "gpt-4o-mini").strip()
    prompt = (
        "你是投研助理。下面编号片段来自用户本机缓存（10-K、新闻、电话会摘录），"
        "可能不完整。请仅用这些片段作答；若不足以回答，请明确说明并提示用户核对原文链接。\n"
        "用中文简洁回答（约 200～400 字），不要编造片段中未出现的事实。\n\n"
        f"用户问题：{q}\n\n"
        "---- 片段 ----\n"
        + "\n\n".join(context_blocks)
    )

    client = OpenAI(api_key=api_key, timeout=90.0, max_retries=0)
    try:
        resp = client.chat.completions.create(
            model=model,
            messages=[{"role": "user", "content": prompt}],
            max_tokens=900,
        )
    except AuthenticationError as e:
        raise RuntimeError("OpenAI 认证失败，请检查 OPENAI_API_KEY。") from e
    except RateLimitError as e:
        raise RuntimeError("OpenAI 请求触发频率限制，请稍后重试。") from e
    except APITimeoutError as e:
        raise RuntimeError("OpenAI 请求超时。") from e
    except APIError as e:
        raise RuntimeError(f"OpenAI API 返回错误: {e}") from e

    answer = (resp.choices[0].message.content or "").strip()
    return {"answer": answer or "（模型返回为空）", "sources": sources}
