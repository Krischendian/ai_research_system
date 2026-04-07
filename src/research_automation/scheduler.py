"""
APScheduler：定时财务批处理、隔夜/昨日报告落盘。

时区默认 ``Asia/Shanghai``（可通过环境变量 ``SCHEDULER_TIMEZONE`` 覆盖，IANA 名称）。
测试：设置 ``SCHEDULER_TEST_MODE=1`` 于启动后约 15s 各执行一次报告任务（不传则仍遵守月初财务规则）。
手动触发（仅供本地）：``SCHEDULER_ENABLE_MANUAL_TRIGGER=1`` 且 ``POST /api/v1/system/scheduler/trigger``。
"""
from __future__ import annotations

import importlib.util
import json
import logging
import os
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Callable
from zoneinfo import ZoneInfo

from apscheduler.schedulers.background import BackgroundScheduler
from apscheduler.triggers.cron import CronTrigger
from apscheduler.triggers.date import DateTrigger

logger = logging.getLogger(__name__)

_ROOT = Path(__file__).resolve().parent.parent.parent
_REPORTS_DIR = _ROOT / "data" / "reports"
_STATUS_FILE = _REPORTS_DIR / "scheduler_last_runs.json"

_sched: BackgroundScheduler | None = None
_started_at_iso: str | None = None

_last_runs: dict[str, dict[str, Any]] = {
    "batch_fetch_financials": {},
    "overnight_report": {},
    "daily_summary_report": {},
}

_dotenv_loaded = False


def _ensure_dotenv() -> None:
    """
    从项目根 ``.env`` 加载变量（``override=False``：已在启动 uvicorn 前 export 的优先级更高）。
    解决仅在 ``curl`` 所在终端 ``export``、而 API 进程未带该变量的问题。
    """
    global _dotenv_loaded
    if _dotenv_loaded:
        return
    try:
        from dotenv import load_dotenv
    except ImportError:
        _dotenv_loaded = True
        return
    load_dotenv(_ROOT / ".env", override=False)
    _dotenv_loaded = True


def _tz() -> ZoneInfo:
    _ensure_dotenv()
    name = (os.getenv("SCHEDULER_TIMEZONE") or "Asia/Shanghai").strip()
    try:
        return ZoneInfo(name)
    except Exception:
        logger.warning("无效的 SCHEDULER_TIMEZONE=%s，回退 Asia/Shanghai", name)
        return ZoneInfo("Asia/Shanghai")


def _report_date_str() -> str:
    """报告文件名日期：调度器时区下的「今天」。"""
    return datetime.now(_tz()).strftime("%Y-%m-%d")


def _ensure_dirs() -> None:
    _REPORTS_DIR.mkdir(parents=True, exist_ok=True)


def _persist_status() -> None:
    _ensure_dirs()
    payload = {
        "last_runs": _last_runs,
        "scheduler_started_at": _started_at_iso,
        "updated_at": datetime.now(timezone.utc).isoformat(),
    }
    try:
        _STATUS_FILE.write_text(
            json.dumps(payload, ensure_ascii=False, indent=2),
            encoding="utf-8",
        )
    except OSError as e:
        logger.warning("写入 scheduler 状态文件失败: %s", e)


def _record(job_id: str, *, ok: bool, detail: str | None = None, exit_code: int | None = None) -> None:
    rec: dict[str, Any] = {
        "at": datetime.now(_tz()).isoformat(),
        "ok": ok,
        "detail": detail,
    }
    if exit_code is not None:
        rec["exit_code"] = exit_code
    _last_runs[job_id] = rec
    _persist_status()


def _load_batch_fetch() -> Callable[..., int]:
    """动态加载 scripts/batch_fetch_financials.py（不在包路径内）。"""
    path = _ROOT / "scripts" / "batch_fetch_financials.py"
    spec = importlib.util.spec_from_file_location("batch_fetch_financials_job", path)
    if spec is None or spec.loader is None:
        raise ImportError(f"无法加载 {path}")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod.run_batch_fetch  # type: ignore[attr-defined]


_run_batch_fetch: Callable[..., int] | None = None


def job_batch_fetch_financials() -> None:
    """每月 1 号 06:00：检查/更新财务（无新数据则脚本内跳过）。"""
    global _run_batch_fetch
    logger.info("调度任务开始: batch_fetch_financials")
    try:
        if _run_batch_fetch is None:
            _run_batch_fetch = _load_batch_fetch()
        code = _run_batch_fetch(ticker=None, force=False)
        ok = code in (0,)
        _record(
            "batch_fetch_financials",
            ok=ok,
            detail=None if ok else f"exit_code={code}",
            exit_code=code,
        )
        logger.info("调度任务结束: batch_fetch_financials exit=%s", code)
    except Exception as e:
        logger.exception("batch_fetch_financials 失败")
        _record("batch_fetch_financials", ok=False, detail=f"{type(e).__name__}: {e}", exit_code=-1)


def job_save_overnight_report() -> None:
    logger.info("调度任务开始: overnight_report")
    try:
        from research_automation.services.overnight_service import get_overnight_news

        data = get_overnight_news()
        _ensure_dirs()
        path = _REPORTS_DIR / f"overnight_{_report_date_str()}.json"
        path.write_text(
            data.model_dump_json(indent=2, ensure_ascii=False),
            encoding="utf-8",
        )
        _record("overnight_report", ok=True, detail=str(path))
        logger.info("隔夜报告已写入 %s", path)
    except Exception as e:
        logger.exception("overnight_report 失败")
        _record("overnight_report", ok=False, detail=f"{type(e).__name__}: {e}")


def job_save_daily_summary_report() -> None:
    logger.info("调度任务开始: daily_summary_report")
    try:
        from research_automation.services.daily_summary_service import get_yesterday_summary

        data = get_yesterday_summary()
        _ensure_dirs()
        path = _REPORTS_DIR / f"daily_{_report_date_str()}.json"
        path.write_text(
            data.model_dump_json(indent=2, ensure_ascii=False),
            encoding="utf-8",
        )
        _record("daily_summary_report", ok=True, detail=str(path))
        logger.info("昨日总结已写入 %s", path)
    except Exception as e:
        logger.exception("daily_summary_report 失败")
        _record("daily_summary_report", ok=False, detail=f"{type(e).__name__}: {e}")


def job_morning_reports() -> None:
    """06:30 顺序执行：先隔夜报告，再昨日总结（避免并发打满 RSS/LLM）。"""
    logger.info("调度任务开始: morning_reports（overnight → daily）")
    job_save_overnight_report()
    job_save_daily_summary_report()


def _restore_status_from_disk() -> None:
    global _started_at_iso
    if not _STATUS_FILE.is_file():
        return
    try:
        raw = json.loads(_STATUS_FILE.read_text(encoding="utf-8"))
        lr = raw.get("last_runs")
        if isinstance(lr, dict):
            for k, v in lr.items():
                if k in _last_runs and isinstance(v, dict):
                    _last_runs[k] = v
        sa = raw.get("scheduler_started_at")
        if isinstance(sa, str):
            _started_at_iso = sa
    except (OSError, json.JSONDecodeError, TypeError) as e:
        logger.warning("读取 scheduler 状态文件失败: %s", e)


def get_scheduler_status_dict() -> dict[str, Any]:
    """供 status API 使用。"""
    _ensure_dotenv()
    tz = _tz()
    keys = (
        "batch_fetch_financials",
        "overnight_report",
        "daily_summary_report",
    )
    last = {k: dict(_last_runs.get(k) or {}) for k in keys}
    return {
        "scheduler_running": _sched is not None and _sched.running,
        "scheduler_timezone": str(tz),
        "scheduler_started_at": _started_at_iso,
        "last_runs": last,
        "reports_dir": str(_REPORTS_DIR.resolve()),
        "manual_trigger_enabled": os.getenv("SCHEDULER_ENABLE_MANUAL_TRIGGER", "")
        .strip()
        .lower()
        in ("1", "true", "yes", "on"),
    }


def trigger_job(job: str) -> dict[str, Any]:
    """
    手动触发（需在环境中开启 SCHEDULER_ENABLE_MANUAL_TRIGGER）。

    ``job``: batch | overnight | daily | reports（仅 overnight+daily）
    """
    _ensure_dotenv()
    enabled = os.getenv("SCHEDULER_ENABLE_MANUAL_TRIGGER", "").strip().lower() in (
        "1",
        "true",
        "yes",
        "on",
    )
    if not enabled:
        return {
            "ok": False,
            "error": (
                "手动触发未启用：请在项目根 .env 中设置 SCHEDULER_ENABLE_MANUAL_TRIGGER=1 "
                "并重启 uvicorn（仅在与 curl 不同的终端 export 不会传入 API 进程）。"
            ),
        }

    j = (job or "").strip().lower()
    try:
        if j == "batch":
            job_batch_fetch_financials()
        elif j == "overnight":
            job_save_overnight_report()
        elif j == "daily":
            job_save_daily_summary_report()
        elif j in ("reports", "all_reports"):
            job_save_overnight_report()
            job_save_daily_summary_report()
        else:
            return {"ok": False, "error": f"未知 job: {job}"}
        return {"ok": True, "job": j}
    except Exception as e:
        logger.exception("手动触发 %s 失败", j)
        return {"ok": False, "error": f"{type(e).__name__}: {e}"}


def start_scheduler() -> None:
    """挂载 cron 并启动后台调度器。"""
    global _sched, _started_at_iso

    _ensure_dotenv()

    if _sched is not None and _sched.running:
        logger.warning("APScheduler 已在运行，跳过重复启动")
        return

    _restore_status_from_disk()
    tz = _tz()
    _started_at_iso = datetime.now(tz).isoformat()

    sched = BackgroundScheduler(timezone=tz)
    # 每月 1 日 06:00 财务批处理
    sched.add_job(
        job_batch_fetch_financials,
        CronTrigger(day=1, hour=6, minute=0, timezone=tz),
        id="batch_fetch_financials",
        replace_existing=True,
    )
    # 每日 06:30：隔夜 + 昨日总结（顺序）
    sched.add_job(
        job_morning_reports,
        CronTrigger(hour=6, minute=30, timezone=tz),
        id="morning_reports",
        replace_existing=True,
    )

    test_mode = os.getenv("SCHEDULER_TEST_MODE", "").strip().lower() in (
        "1",
        "true",
        "yes",
        "on",
    )
    if test_mode:
        fire_at = datetime.now(tz) + timedelta(seconds=15)
        sched.add_job(
            job_morning_reports,
            DateTrigger(run_date=fire_at),
            id="test_morning_reports_once",
            replace_existing=True,
        )
        logger.warning(
            "SCHEDULER_TEST_MODE 已开启：将于 %s 左右顺序执行隔夜+昨日总结",
            fire_at.isoformat(),
        )

    sched.start()
    _sched = sched
    _persist_status()
    logger.info(
        "APScheduler 已启动 timezone=%s reports=%s",
        tz,
        _REPORTS_DIR,
    )


def shutdown_scheduler() -> None:
    global _sched
    if _sched is not None:
        try:
            _sched.shutdown(wait=False)
        except Exception as e:
            logger.warning("关闭 scheduler 时异常: %s", e)
        _sched = None
    logger.info("APScheduler 已关闭")
