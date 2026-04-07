"""系统状态与调度手动触发。"""
from __future__ import annotations

from typing import Any

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel, Field

from research_automation.models.system import JobLastRun, SystemStatusResponse
from research_automation.scheduler import get_scheduler_status_dict, trigger_job

router = APIRouter(prefix="/system", tags=["system"])


def _last_runs_to_models(raw: dict[str, Any]) -> dict[str, JobLastRun]:
    out: dict[str, JobLastRun] = {}
    for key, val in raw.items():
        if not isinstance(val, dict):
            out[key] = JobLastRun()
            continue
        out[key] = JobLastRun(
            at=val.get("at"),
            ok=val.get("ok"),
            detail=val.get("detail"),
            exit_code=val.get("exit_code"),
        )
    return out


@router.get("/status", response_model=SystemStatusResponse)
def system_status() -> SystemStatusResponse:
    """返回调度器是否运行、时区、各任务最后一次执行时间等。"""
    d = get_scheduler_status_dict()
    lr_raw = d.get("last_runs")
    if not isinstance(lr_raw, dict):
        lr_raw = {}
    return SystemStatusResponse(
        scheduler_running=bool(d.get("scheduler_running")),
        scheduler_timezone=str(d.get("scheduler_timezone") or ""),
        scheduler_started_at=d.get("scheduler_started_at"),
        last_runs=_last_runs_to_models(lr_raw),
        reports_dir=str(d.get("reports_dir") or ""),
        manual_trigger_enabled=bool(d.get("manual_trigger_enabled")),
    )


class SchedulerTriggerBody(BaseModel):
    """手动触发任务名。"""

    job: str = Field(
        ...,
        description="batch | overnight | daily | reports（隔夜+昨日总结接连执行）",
    )


@router.post("/scheduler/trigger")
def scheduler_trigger(body: SchedulerTriggerBody) -> dict[str, Any]:
    """
    手动执行调度任务（需环境变量 ``SCHEDULER_ENABLE_MANUAL_TRIGGER=1``）。
    """
    result = trigger_job(body.job)
    if not result.get("ok"):
        err = result.get("error") or "触发失败"
        if "未启用" in err:
            raise HTTPException(status_code=403, detail=err) from None
        if "未知" in err:
            raise HTTPException(status_code=400, detail=err) from None
        raise HTTPException(status_code=500, detail=err) from None
    return result
