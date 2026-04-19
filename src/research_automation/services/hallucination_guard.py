"""情态词漂移检测（L3 防幻觉）。"""
from __future__ import annotations

import logging
from typing import Any

logger = logging.getLogger(__name__)

CERTAINTY_WORDS_CN = ["开始", "已经", "正在", "完成", "实现", "建立"]
MODAL_WORDS_EN = [
    "expect", "plan", "may", "consider", "intend", "target", "anticipate",
]

MODAL_THRESHOLD = 2
CERTAIN_THRESHOLD = 2


def check_modality_drift(summary_cn: str, source_en: str) -> dict:
    source_lower = source_en.lower()

    modal_hits = [w for w in MODAL_WORDS_EN if w in source_lower]
    modal_count = len(modal_hits)

    certain_hits = [w for w in CERTAINTY_WORDS_CN if w in summary_cn]
    certain_count = len(certain_hits)

    has_drift = (modal_count >= MODAL_THRESHOLD) and (certain_count >= CERTAIN_THRESHOLD)

    return {
        "has_drift": has_drift,
        "risk_level": "HIGH" if has_drift else "LOW",
        "flagged_phrases": certain_hits,
        "modal_hits": modal_hits,
        "modal_count": modal_count,
        "certain_count": certain_count,
    }


def flag_if_drifted(field_name: str, summary_cn: str, source_en: str) -> bool:
    result = check_modality_drift(summary_cn, source_en)
    if result["has_drift"]:
        logger.warning(
            "情态词漂移 field=%s risk=%s certain_hits=%s modal_hits=%s",
            field_name,
            result["risk_level"],
            result["flagged_phrases"],
            result["modal_hits"],
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
