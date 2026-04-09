"""
财报电话会逐字稿入口（历史模块名保留）。

实际数据仅通过 ``earningscall_lib.get_transcript_from_earningscall`` 获取；
本文件不再提供 Mock 文本。
"""
from __future__ import annotations

import logging

logger = logging.getLogger(__name__)


def _fetch_from_bloomberg(ticker: str, year: int, quarter: int) -> None:
    """
    预留：从 Bloomberg 获取财报电话会原文。

    未实现，请勿在生产环境依赖。
    """
    del ticker, year, quarter
    pass


def get_transcript(ticker: str, year: int, quarter: int) -> str:
    """
    已废弃：项目已移除 Mock 逐字稿，调用方须改用 earningscall。

    保留函数签名仅为避免旧代码静默失败；调用将抛出 ``NotImplementedError``。
    """
    del ticker, year, quarter
    raise NotImplementedError(
        "Mock 逐字稿已移除，请使用 earningscall_lib.get_transcript_from_earningscall"
    )
