"""
财报电话会逐字稿抓取。

当前为 **Mock** 数据；预留 Bloomberg 接入点。
"""
from __future__ import annotations

import logging

logger = logging.getLogger(__name__)

# --- Mock：AAPL 2024Q4 风格示例（虚构，仅用于联调） ---
_MOCK_AAPL_2024_Q4 = """
Operator: Good day and welcome to the Apple Inc. Q4 2024 Earnings Conference Call.
Tim Cook, Chief Executive Officer, and Luca Maestri, Chief Financial Officer, will discuss results.

Tim Cook — CEO: Thanks everyone. We are pleased with record revenue for the quarter, with strength
across iPhone and Services. Customers have responded well to our latest iPhone lineup. We also saw
continued double-digit growth in our installed base of active devices.

Luca Maestri — CFO: Total company revenue was $94.9 billion, up 6 percent year over year. Services
revenue reached a new all-time record. We expect Services revenue to grow double digits year over
year in the December quarter on a reported basis.

Tim Cook — CEO: We remain very optimistic about our opportunity in generative AI and are investing
meaningfully in both models and silicon. We plan to ship new software features that leverage on-device
and private cloud processing, consistent with our privacy values.

Luca Maestri — CFO: Gross margin came in at 46.2 percent, consistent with our guidance range. We also
returned over $29 billion to shareholders through dividends and repurchases during the quarter.

Tim Cook — CEO: Geographically, we saw solid performance in emerging markets, while Greater China
remained a focus area for competitive dynamics and we are working to extend our retail footprint.

Operator: We'll now take questions from analysts.
""".strip()


def _fetch_from_bloomberg(ticker: str, year: int, quarter: int) -> None:
    """
    预留：从 Bloomberg 获取财报电话会原文。

    未实现，请勿在生产环境依赖。
    """
    # Bloomberg API / 终端导出接口待接入
    del ticker, year, quarter
    pass


def get_transcript(ticker: str, year: int, quarter: int) -> str:
    """
    获取指定标的、年份、季度的电话会逐字稿 **纯文本**。

    POC 阶段：对任意 ticker 均返回同一份 **Mock** 示例（标注为 AAPL 2024Q4 风格），
    便于前后端联调；正式接入后应改为远端抓取并在此处分支调用 ``_fetch_from_bloomberg`` 等。
    """
    sym = (ticker or "").strip().upper()
    if not sym:
        return ""

    # 正式管线示例：if use_bloomberg: return _fetch_from_bloomberg(...)
    logger.debug(
        "earnings transcript (mock) ticker=%s year=%s quarter=%s",
        sym,
        year,
        quarter,
    )

    header = (
        f"[MOCK TRANSCRIPT] Symbol={sym} Fiscal period label={year}Q{quarter} "
        f"(below body is hardcoded demo text styled as AAPL 2024Q4)\n\n"
    )
    return header + _MOCK_AAPL_2024_Q4
