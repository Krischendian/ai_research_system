"""
earningscall 逐字稿拉取冒烟测试：需网络。在项目根目录执行：

    python tests/test_earningscall.py

（脚本会自动把 ``src`` 加入 ``sys.path``；若未自动加入，可用 ``PYTHONPATH=src``，注意是 **PYTHON** 不是 YTHON。）
"""
from __future__ import annotations

import sys
from pathlib import Path

# 与 scripts/batch_fetch_financials.py 一致，避免未设置 PYTHONPATH 时找不到包
_ROOT = Path(__file__).resolve().parents[1]
_SRC = _ROOT / "src"
if str(_SRC) not in sys.path:
    sys.path.insert(0, str(_SRC))

from research_automation.extractors.earningscall_lib import get_transcript_from_earningscall


def main() -> None:
    text = get_transcript_from_earningscall("AAPL", 2024, 4)
    if text is None:
        print("未获取到逐字稿（见上方日志）")
        return
    preview = text[:500]
    print(f"长度={len(text)} 字符，前 500 字符：\n{preview}")


if __name__ == "__main__":
    main()
