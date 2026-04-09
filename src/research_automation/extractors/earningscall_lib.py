"""
通过第三方库 ``earningscall`` 拉取财报电话会逐字稿（公开数据源）。
"""
from __future__ import annotations

import logging
from typing import Any

logger = logging.getLogger(__name__)


def get_transcript_from_earningscall(
    ticker: str, year: int, quarter: int
) -> str | None:
    """
    获取指定标的、财年、季度的电话会**纯文本**逐字稿。

    :param ticker: 股票代码，如 ``AAPL``
    :param year: 财年公历年，如 ``2024``
    :param quarter: 财季 ``1``～``4``
    :return: 成功返回非空字符串；无数据或失败返回 ``None``（并打日志）。
    """
    sym = (ticker or "").strip().upper()
    if not sym:
        logger.warning("earningscall：ticker 为空，跳过拉取")
        return None
    if quarter < 1 or quarter > 4:
        logger.warning(
            "earningscall：quarter=%s 非法（须为 1～4），ticker=%s",
            quarter,
            sym,
        )
        return None

    # 延迟导入：未安装依赖时仍可加载本模块，由调用方走 Mock 回退
    try:
        from earningscall import get_company
    except ImportError as e:
        logger.warning(
            "earningscall：未安装 earningscall 库（%s）。请在已激活的 venv 中执行："
            " pip install earningscall  或  pip install -r requirements.txt",
            e,
        )
        return None

    try:
        company: Any = get_company(sym)
        # 无对应季度时库返回 None
        transcript = company.get_transcript(year=year, quarter=quarter)
    except Exception as exc:  # noqa: BLE001
        logger.warning(
            "earningscall：网络或 API 错误 ticker=%s year=%s quarter=%s: %s",
            sym,
            year,
            quarter,
            exc,
        )
        return None

    if transcript is None:
        logger.warning(
            "earningscall：无逐字稿 ticker=%s year=%s quarter=%s",
            sym,
            year,
            quarter,
        )
        return None

    text = getattr(transcript, "text", None)
    if text is None:
        logger.warning(
            "earningscall：transcript 无 text 属性 ticker=%s year=%s quarter=%s",
            sym,
            year,
            quarter,
        )
        return None

    out = str(text).strip()
    if not out:
        logger.warning(
            "earningscall：逐字稿为空字符串 ticker=%s year=%s quarter=%s",
            sym,
            year,
            quarter,
        )
        return None

    return out
