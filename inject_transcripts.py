"""
把 Bloomberg PDF 逐字稿/摘要注入 earnings step_cache，
绕过 FMP/SEC 拉取，直接用 LLM 生成分析并缓存。
"""
import sys
import json
import pdfplumber
 
PDF_MAP = {
    "RTO": {
        "path": "/Users/krisfan/Desktop/20260416_Rentokil_Initial_PLC-_Sales_Results_Call_2026-4-16_DN000000003103977885.pdf.pdf",
        "year": 2026,
        "quarter": 1,
        "source": "sec_8k",
    },
    "DHL GY": {
        "path": "/Users/krisfan/Desktop/Deutsche Post AG Earnings Call Summary.pdf",
        "year": 2026,
        "quarter": 1,
        "source": "sec_8k",
    },
    "BT/A LN": {
        "path": "/Users/krisfan/Desktop/BT Group PLC Earnings Call Summary.pdf",
        "year": 2025,
        "quarter": 4,
        "source": "sec_8k",
    },
}
 
def extract_text(path):
    with pdfplumber.open(path) as pdf:
        return "\n".join(p.extract_text() or "" for p in pdf.pages)
 
def main():
    # 添加项目路径
    sys.path.insert(0, "src")
 
    from research_automation.core.paragraph_text import (
        build_earnings_doc_uid,
        split_into_paragraphs,
        make_earnings_records,
        paragraphs_to_numbered_excerpt,
        index_to_paragraph_id_map,
        all_paragraph_id_set,
    )
    from research_automation.core.database import (
        replace_document_paragraphs,
        set_step_cache,
    )
    from research_automation.services.earnings_service import (
        _build_prompt,
        _extract_json_object,
        _parse_viewpoints,
        _quote_in_transcript,
        _collect_earnings_cited_ids,
    )
    _EARNINGS_CACHE_VERSION = 3  # 与 earnings_service.py 保持一致
    from research_automation.core.paragraph_refs import normalize_paragraph_ref_list
    from research_automation.extractors.llm_client import chat
    from research_automation.models.earnings import (
        EarningsCallAnalysis,
        EarningsQuotation,
        EarningsViewpoint,
    )
    from datetime import datetime, timezone
 
    for ticker, cfg in PDF_MAP.items():
        year = cfg["year"]
        quarter = cfg["quarter"]
        qlabel = f"{year}Q{quarter}"
        print(f"\n{'='*50}")
        print(f"处理: {ticker} {qlabel}")
 
        # 提取文本
        transcript = extract_text(cfg["path"]).strip()
        print(f"  文本长度: {len(transcript)} chars")
 
        # 分段入库
        doc_uid = build_earnings_doc_uid(ticker, year, quarter)
        chunks = split_into_paragraphs(transcript)
        records = make_earnings_records(doc_uid, chunks)
        replace_document_paragraphs(
            doc_uid, ticker, "EARNINGS_CALL",
            context_year=year, quarter_label=qlabel,
            records=[(r["paragraph_id"], r["para_index"], r["content"]) for r in records],
        )
        numbered = paragraphs_to_numbered_excerpt(records)
        idx_to_full = index_to_paragraph_id_map(records)
        allowed = all_paragraph_id_set(records)
        id_to_text = {r["paragraph_id"]: r["content"] for r in records}
 
        # 调 LLM
        prompt = _build_prompt(ticker, qlabel, numbered)
        print(f"  调用 LLM...")
        try:
            reply = chat(prompt, response_format={"type": "json_object"}, timeout=240.0, max_tokens=8192)
        except Exception as e:
            print(f"  LLM 失败: {e}")
            continue
 
        try:
            payload = _extract_json_object(reply)
        except Exception as e:
            print(f"  JSON 解析失败: {e}")
            continue
 
        from research_automation.core.paragraph_refs import normalize_paragraph_ref_list
        summary = str(payload.get("summary", "") or "").strip()
        summary_ids = normalize_paragraph_ref_list(
            payload.get("summary_source_paragraph_ids"),
            idx_to_full=idx_to_full, allowed=allowed,
        )
        viewpoints = _parse_viewpoints(
            payload.get("management_viewpoints"),
            idx_to_full=idx_to_full, allowed=allowed,
        )
        nbus = _parse_viewpoints(
            payload.get("new_business_highlights"),
            idx_to_full=idx_to_full, allowed=allowed,
        )
        quotations = []
        for it in (payload.get("quotations") or []):
            if not isinstance(it, dict):
                continue
            q = str(it.get("quote", "")).strip()
            if not q or not _quote_in_transcript(q, transcript):
                continue
            qids = normalize_paragraph_ref_list(
                it.get("source_paragraph_ids"),
                idx_to_full=idx_to_full, allowed=allowed,
            )
            quotations.append(EarningsQuotation(
                speaker=str(it.get("speaker", "") or "").strip(),
                quote=q, topic=str(it.get("topic", "") or "").strip(),
                source_paragraph_ids=qids,
            ))
 
        cited = _collect_earnings_cited_ids(summary_ids, viewpoints, quotations, nbus)
        source_paragraphs = {pid: id_to_text[pid] for pid in cited if pid in id_to_text}
 
        ds_label = f"逐字稿来源：Bloomberg Terminal PDF；分析由 LLM 基于该文本生成。"
 
        result = EarningsCallAnalysis(
            ticker=ticker, quarter=qlabel, summary=summary,
            summary_source_paragraph_ids=summary_ids,
            management_viewpoints=viewpoints, quotations=quotations,
            new_business_highlights=nbus,
            last_updated=datetime.now(timezone.utc).isoformat(),
            data_source=cfg["source"], data_source_label=ds_label,
            document_uid=doc_uid, source_paragraphs=source_paragraphs,
        )
 
        set_step_cache(
            sector="__earnings__", year=year, quarter=quarter,
            step="step4_analysis", ticker=ticker,
            content=result.model_dump_json(),
            cache_version=_EARNINGS_CACHE_VERSION,
        )
        print(f"  ✓ 缓存写入成功: {ticker} {qlabel}")
        print(f"  summary: {summary[:100]}...")
 
    print("\n全部完成！")
 
if __name__ == "__main__":
    main()
 