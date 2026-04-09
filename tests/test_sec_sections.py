"""SEC 10-K 多章节解析：联网时用真实 AAPL 申报做烟测。"""
from __future__ import annotations

import unittest

from research_automation.extractors.sec_edgar import SecEdgarError, get_10k_sections


class TestGet10kSectionsLive(unittest.TestCase):
    """需要访问 SEC；失败时 skip，不阻断离线 CI。"""

    def test_aapl_2023_sections_print_snippets(self) -> None:
        try:
            sections = get_10k_sections("AAPL", 2023)
        except SecEdgarError as e:
            self.skipTest(f"SEC 不可用：{e}")

        for key in ("item1", "item1a", "item7", "item8_notes"):
            body = sections.get(key, "")
            preview = (body[:500] + "…") if len(body) > 500 else body
            print(f"\n--- {key} (len={len(body)}) ---\n{preview}")
            self.assertIsInstance(body, str)
            # Item 1 在正规 10-K 中应显著较长；其余章节因发行人可能极长或版式差异，仅作弱断言
            if key == "item1":
                self.assertGreater(
                    len(body.strip()),
                    500,
                    "AAPL Item 1 应能解析出足够长度",
                )
            if key == "item7":
                self.assertGreater(
                    len(body.strip()),
                    5000,
                    "AAPL Item 7 MD&A 不应被 Item 8 交叉引用误截断",
                )


if __name__ == "__main__":
    unittest.main()
