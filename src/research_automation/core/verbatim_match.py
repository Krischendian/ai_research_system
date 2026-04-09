"""
逐字摘录校验：轻量归一化子串匹配 + 可选模糊匹配（空白/标点/大小写差异）。

- ``normalize_for_verbatim_substring``：保留大小写，适合「尽量逐字」的英文子串判断。
- ``normalize_text`` / ``is_similar``：转小写、弱化标点，用于容忍模型与原文轻微偏差。
"""

from __future__ import annotations

import re
from difflib import SequenceMatcher


def normalize_for_verbatim_substring(text: str) -> str:
    if not text:
        return ""
    t = text.replace("\u201c", '"').replace("\u201d", '"')  # “ ”
    t = t.replace("\u2018", "'").replace("\u2019", "'")  # ‘ ’
    t = t.replace("\u2032", "'")
    t = t.replace("\u00a0", " ").replace("\u200b", "").replace("\ufeff", "")
    t = t.replace("…", "...")
    return " ".join(t.split())


def collapse_whitespace(text: str) -> str:
    return " ".join((text or "").split())


def normalize_text(text: str) -> str:
    """
    归一化：标点改为空白、压缩空白、转小写（仅用于模糊匹配，不用于展示）。
    """
    t = (text or "").replace("\u00a0", " ")
    t = re.sub(r"[^\w\s]", " ", t, flags=re.UNICODE)
    t = re.sub(r"\s+", " ", t)
    return t.lower().strip()


def is_similar(quote: str, paragraph: str, threshold: float = 0.95) -> bool:
    """
    判断 ``quote`` 是否与 ``paragraph`` 足够相似（归一化后子串或 ``SequenceMatcher``）。

    若 ``paragraph`` 明显长于 ``quote``，仅在归一化后 ``quote`` 为 ``paragraph`` 子串时判真；
    长度接近时用语义上的字符级相似度（容忍少量笔误/截断）。
    """
    nq = normalize_text(quote)
    np = normalize_text(paragraph)
    if not nq or not np:
        return False
    if nq == np:
        return True
    if nq in np:
        return True
    if len(np) < len(nq) * 0.85:
        return False
    if len(np) > len(nq) * 2.8:
        return False
    return SequenceMatcher(None, nq, np, autojunk=False).ratio() >= threshold


def _strip_outer_quotes(s: str) -> str:
    raw = (s or "").strip()
    if len(raw) >= 2 and raw[0] == raw[-1] and raw[0] in "\"'":
        return raw[1:-1].strip()
    return raw


def quote_matches_haystack(
    quote: str,
    haystack: str,
    *,
    similarity_threshold: float = 0.9,
) -> bool:
    """
    判断 ``quote`` 是否出现在 ``haystack`` 中：先逐字归一化子串，再整段归一化子串，
    再按段落做 ``is_similar``（阈值略低于默认，便于长段内轻微差异）。
    """
    raw = _strip_outer_quotes(quote)
    if not raw.strip():
        return False

    qv = normalize_for_verbatim_substring(raw)
    if qv:
        if qv in haystack:
            return True
        ev = normalize_for_verbatim_substring(haystack)
        if qv in ev:
            return True
        if collapse_whitespace(qv) in collapse_whitespace(ev):
            return True

    nt_q = normalize_text(raw)
    nt_h = normalize_text(haystack)
    if nt_q and nt_q in nt_h:
        return True

    para_threshold = min(0.94, max(0.82, similarity_threshold))
    for para in re.split(r"\n\s*\n+", haystack):
        p = para.strip()
        if not p:
            continue
        if is_similar(raw, p, threshold=para_threshold):
            return True
    return False
