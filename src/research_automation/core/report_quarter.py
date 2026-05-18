"""行业报告缓存季（与 sector_report_service / 前端一致）。"""
from __future__ import annotations

from datetime import datetime, timezone


def current_report_cache_quarter(
    *,
    now: datetime | None = None,
) -> tuple[int, int]:
    """上一完整财季 (year, quarter)，用于 sector_report_cache 键。"""
    dt = now or datetime.now(timezone.utc)
    q = (dt.month - 1) // 3 + 1
    y = dt.year
    if q == 1:
        return y - 1, 4
    return y, q - 1
