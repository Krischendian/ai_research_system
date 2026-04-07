"""系统状态与调度相关 API 模型。"""
from __future__ import annotations

from typing import Any, Optional

from pydantic import BaseModel, Field


class JobLastRun(BaseModel):
    """单次任务最后执行记录。"""

    at: Optional[str] = None  # ISO8601，成功或失败均更新时间
    ok: Optional[bool] = None
    detail: Optional[str] = None
    exit_code: Optional[int] = None  # 仅 batch 任务有


class SystemStatusResponse(BaseModel):
    """GET /api/v1/system/status"""

    scheduler_running: bool = False
    scheduler_timezone: str = ""
    scheduler_started_at: Optional[str] = None
    last_runs: dict[str, JobLastRun] = Field(default_factory=dict)
    reports_dir: str = ""
    manual_trigger_enabled: bool = False
