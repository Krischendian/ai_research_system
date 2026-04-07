"""将披露/逐字稿正文拆为段落并生成稳定段落 ID。"""
from __future__ import annotations

import re
from typing import TypedDict


class ParagraphRecord(TypedDict):
    """单段：供入库与 Prompt 标注。"""

    paragraph_id: str
    para_index: int
    content: str


def split_into_paragraphs(text: str, *, max_paragraphs: int = 2500) -> list[str]:
    """按空行分段；去掉仅空白片段；段数过多时将尾部合并为一段。"""
    raw = (text or "").replace("\r\n", "\n")
    parts = re.split(r"\n\s*\n+", raw)
    chunks: list[str] = [p.strip() for p in parts if p and p.strip()]
    if not chunks:
        single = raw.strip()
        return [single] if single else []
    if len(chunks) > max_paragraphs:
        head = chunks[: max_paragraphs - 1]
        tail = "\n\n".join(chunks[max_paragraphs - 1 :])
        chunks = head + [tail]
    return chunks


def build_10k_item1_doc_uid(ticker: str, filing_year: int) -> str:
    sym = (ticker or "").strip().upper()
    return f"{sym}_10K_{filing_year}_Item1"


def build_10k_paragraph_id(doc_uid: str, para_index: int) -> str:
    """para_index 从 1 起。"""
    return f"{doc_uid}_para_{para_index}"


def build_earnings_doc_uid(ticker: str, year: int, quarter: int) -> str:
    sym = (ticker or "").strip().upper()
    return f"{sym}_EC_{year}Q{quarter}"


def build_earnings_paragraph_id(doc_uid: str, para_index: int) -> str:
    return f"{doc_uid}_para_{para_index}"


def paragraphs_to_numbered_excerpt(records: list[ParagraphRecord]) -> str:
    """供 LLM：每段带 PARAGRAPH_ID 行。"""
    lines: list[str] = []
    for r in records:
        pid = r["paragraph_id"]
        body = (r["content"] or "").strip()
        if not body:
            continue
        lines.append(f"PARAGRAPH_ID: {pid}")
        lines.append(body)
        lines.append("")
    return "\n".join(lines).strip()


def index_to_paragraph_id_map(records: list[ParagraphRecord]) -> dict[int, str]:
    return {r["para_index"]: r["paragraph_id"] for r in records}


def all_paragraph_id_set(records: list[ParagraphRecord]) -> set[str]:
    return {r["paragraph_id"] for r in records}


def make_10k_records(doc_uid: str, chunks: list[str]) -> list[ParagraphRecord]:
    return [
        ParagraphRecord(
            paragraph_id=build_10k_paragraph_id(doc_uid, i),
            para_index=i,
            content=c,
        )
        for i, c in enumerate(chunks, start=1)
    ]


def make_earnings_records(doc_uid: str, chunks: list[str]) -> list[ParagraphRecord]:
    return [
        ParagraphRecord(
            paragraph_id=build_earnings_paragraph_id(doc_uid, i),
            para_index=i,
            content=c,
        )
        for i, c in enumerate(chunks, start=1)
    ]
