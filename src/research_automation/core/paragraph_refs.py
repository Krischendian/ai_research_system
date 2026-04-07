"""LLM 返回的段落引用（p42 / 完整 ID）规范化为库内段落主键。"""
from __future__ import annotations

import re
from typing import Any


def normalize_paragraph_ref_list(
    raw: Any,
    *,
    idx_to_full: dict[int, str],
    allowed: set[str],
) -> list[str]:
    if not isinstance(raw, list):
        return []
    out: list[str] = []
    for x in raw:
        s = str(x).strip()
        if not s:
            continue
        m = re.fullmatch(r"(?i)p(\d+)", s)
        if m:
            idx = int(m.group(1))
            pid = idx_to_full.get(idx)
            if pid and pid in allowed:
                out.append(pid)
            continue
        if s in allowed:
            out.append(s)
    dedup: list[str] = []
    seen: set[str] = set()
    for p in out:
        if p not in seen:
            seen.add(p)
            dedup.append(p)
    return dedup
