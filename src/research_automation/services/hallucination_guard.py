"""情态词漂移检测（L3 防幻觉）。"""
from __future__ import annotations

import logging
from typing import Any

logger = logging.getLogger(__name__)

CERTAINTY_WORDS_CN = ["开始", "已经", "正在", "完成", "实现", "建立"]
MODAL_WORDS_EN = [
    "expect",
    "plan",
    "may",
    "consider",
    "intend",
    "target",
    "anticipate",
]


def check_modality_drift(summary_cn: str, source_en: str) -> dict:
    has_modal_source = any(w in source_en.lower() for w in MODAL_WORDS_EN)
    has_certain_summary = any(w in summary_cn for w in CERTAINTY_WORDS_CN)
    if has_modal_source and has_certain_summary:
        return {
            "has_drift": True,
            "risk_level": "HIGH",
            "flagged_phrases": CERTAINTY_WORDS_CN,
        }
    return {"has_drift": False, "risk_level": "LOW", "flagged_phrases": []}


def flag_if_drifted(field_name: str, summary_cn: str, source_en: str) -> bool:
    result = check_modality_drift(summary_cn, source_en)
    if result["has_drift"]:
        logger.warning(
            "情态词漂移 field=%s risk=%s flagged=%s",
            field_name,
            result["risk_level"],
            result["flagged_phrases"],
        )
        return True
    return False


def check_profile_fields(payload: dict[str, Any], source_text: str = "") -> None:
    fields_to_check = ["future_guidance", "industry_view"]
    for field in fields_to_check:
        val = payload.get(field)
        if not val or not isinstance(val, str):
            continue
        if val in ("原文未明确提及", "NOT_FOUND"):
            continue
        flag_if_drifted(field, val, source_text)
