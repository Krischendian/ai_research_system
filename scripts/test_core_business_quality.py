"""
批量检查所有监控公司的 core_business 质量。
从仓库根目录运行: PYTHONPATH=src python3 scripts/test_core_business_quality.py
"""
import logging
import sys
from pathlib import Path

_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(_ROOT / "src"))
logging.basicConfig(level=logging.WARNING)

from dotenv import load_dotenv

load_dotenv(_ROOT / ".env")

from research_automation.services.profile_service import (  # noqa: E402
    ProfileGenerationError,
    get_profile,
)

AI_SECTOR = [
    "CTSH", "AVY", "IBM", "PPG", "JLL", "ACN", "EL", "TGT",
    "UPS", "DG", "HCA", "BAH", "MDB", "ZM", "RTO",
    "BT/A LN", "FRE GY", "KBX GY", "DHL GY",
]

GAS_SECTOR = [
    "PUMP", "LBRT", "SEI", "NOV", "BKR", "SLB", "HAL",
    "ETR", "ATO", "NFG", "NWN", "TE",
]

_INVALID_KW = (
    "无法", "未提供", "未包含", "未见", "不可用", "原文未明确提及",
    "NOT_FOUND", "高管", "履历", "组织架构", "注册", "子公司运营",
)


def check_quality(cb: str) -> str:
    if not cb or len(cb) < 30:
        return "❌ 空/过短"
    if any(kw in cb for kw in _INVALID_KW):
        return "⚠️  内容无效"
    if "FMP Revenue Segmentation" in cb:
        return "🔄 FMP fallback"
    return "✅ 正常"


def main() -> None:
    print("\n=== AI job replacement ===")
    for ticker in AI_SECTOR:
        try:
            profile = get_profile(ticker)
            cb = (profile.core_business or "").strip()
            status = check_quality(cb)
            preview = cb[:80].replace("\n", " ")
            print(f"{status} {ticker:12} {preview}")
        except ProfileGenerationError as e:
            print(f"❌ {ticker:12} ProfileError: {e.message[:60]}")
        except Exception as e:
            print(f"❌ {ticker:12} Error: {str(e)[:60]}")

    print("\n=== Natural Gas ===")
    for ticker in GAS_SECTOR:
        try:
            profile = get_profile(ticker)
            cb = (profile.core_business or "").strip()
            status = check_quality(cb)
            preview = cb[:80].replace("\n", " ")
            print(f"{status} {ticker:12} {preview}")
        except ProfileGenerationError as e:
            print(f"❌ {ticker:12} ProfileError: {e.message[:60]}")
        except Exception as e:
            print(f"❌ {ticker:12} Error: {str(e)[:60]}")


if __name__ == "__main__":
    main()
