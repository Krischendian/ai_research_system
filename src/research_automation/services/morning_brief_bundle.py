"""晨报产品用例：聚合隔夜、昨日总结与经典晨报。"""
from __future__ import annotations

import logging

from research_automation.models.news import MorningBrief
from research_automation.models.product import MorningBriefBundleResponse
from research_automation.services.daily_summary_service import get_yesterday_summary
from research_automation.services.news_service import NewsBriefError, get_morning_brief
from research_automation.services.overnight_service import get_overnight_news

logger = logging.getLogger(__name__)


def get_morning_brief_bundle(
    sector: str | None = None,
    *,
    include_classic_brief: bool = True,
) -> MorningBriefBundleResponse:
    """
    拉取晨报 UI 所需的全部新闻块（单用例入口）。

    ``sector`` 传给隔夜/昨日总结以过滤监控池相关公司新闻。
    """
    sec = (sector or "").strip() or None
    overnight = get_overnight_news(sector=sec)
    yesterday = get_yesterday_summary(sector=sec)
    classic: MorningBrief | None = None
    if include_classic_brief:
        try:
            classic = get_morning_brief()
        except NewsBriefError as e:
            logger.warning("经典晨报块失败（bundle 仍返回隔夜/昨日）: %s", e.message)
    return MorningBriefBundleResponse(
        sector=sec,
        overnight=overnight,
        yesterday_summary=yesterday,
        morning_brief=classic,
    )
