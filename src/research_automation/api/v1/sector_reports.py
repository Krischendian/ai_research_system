"""行业六步报告（产品级 API）。"""
from __future__ import annotations

from typing import Annotated

from fastapi import APIRouter, HTTPException, Path, Query

from research_automation.api.openapi_meta import COMMON_ERROR_RESPONSES
from research_automation.core.database import get_connection, init_db
from research_automation.core.report_quarter import current_report_cache_quarter
from research_automation.models.product import SectorReportCreateRequest, SectorReportResponse
from research_automation.services.report_cache import get_cached_report, has_cached_report
from research_automation.services.sector_report_service import generate_six_step_sector_report

router = APIRouter(prefix="/sector-reports", tags=["sector-reports"])


def _distinct_sectors() -> list[str]:
    conn = get_connection()
    try:
        init_db(conn)
        cur = conn.execute(
            "SELECT DISTINCT sector FROM companies "
            "WHERE is_active = 1 AND TRIM(sector) != '' ORDER BY sector"
        )
        return [str(r[0]).strip() for r in cur.fetchall() if r[0]]
    finally:
        conn.close()


def _resolve_year_quarter(
    year: int | None,
    quarter: int | None,
) -> tuple[int, int]:
    if year is not None and quarter is not None:
        if quarter < 1 or quarter > 4:
            raise HTTPException(status_code=400, detail="quarter 须在 1–4 之间")
        return int(year), int(quarter)
    return current_report_cache_quarter()


@router.get(
    "/sectors",
    response_model=list[str],
    operation_id="list_sector_report_sectors",
    summary="②-a 行业报告 — 板块列表",
    description="`companies` 表中 `is_active=1` 的 distinct `sector`，供前端下拉框。",
)
def list_sectors() -> list[str]:
    return _distinct_sectors()


@router.get(
    "/{sector}",
    response_model=SectorReportResponse,
    operation_id="get_sector_report_cached",
    summary="②-b 行业报告 — 读缓存",
    description="默认上一完整财季；无缓存 404。命中后仍会刷新季度图数据。",
    responses={
        404: {
            "description": "该 sector/year/quarter 无缓存",
            "content": {"application/json": {"example": {"detail": "无缓存报告"}}},
        },
        500: COMMON_ERROR_RESPONSES[500],
    },
)
def get_sector_report(
    sector: Annotated[str, Path(description="板块名称")],
    year: int | None = Query(None, description="缓存年；默认上一完整财季"),
    quarter: int | None = Query(None, ge=1, le=4, description="缓存季 1–4"),
    probe_only: bool = Query(
        False,
        description="仅探测 SQLite 是否有缓存（毫秒级，不返回正文）",
    ),
) -> SectorReportResponse:
    sec = (sector or "").strip()
    if not sec:
        raise HTTPException(status_code=400, detail="sector 不能为空")
    y, q = _resolve_year_quarter(year, quarter)
    if probe_only:
        if not has_cached_report(sec, y, q):
            raise HTTPException(
                status_code=404,
                detail=f"无缓存报告：{sec} {y}Q{q}，请 POST 生成",
            )
        return SectorReportResponse(
            sector=sec,
            year=y,
            quarter=q,
            report_md="",
            quarterly_data={},
            from_cache=True,
        )
    cached_md = get_cached_report(sec, y, q)
    if not cached_md:
        raise HTTPException(
            status_code=404,
            detail=f"无缓存报告：{sec} {y}Q{q}，请 POST 生成",
        )
    return SectorReportResponse(
        sector=sec,
        year=y,
        quarter=q,
        report_md=cached_md,
        quarterly_data={},
        from_cache=True,
    )


@router.post(
    "",
    response_model=SectorReportResponse,
    operation_id="create_sector_report",
    summary="②-c 行业报告 — 生成",
    description="同步执行 `generate_six_step_sector_report`；`force_refresh=true` 时忽略 SQLite 整份缓存。",
    responses={500: COMMON_ERROR_RESPONSES[500]},
)
def create_sector_report(body: SectorReportCreateRequest) -> SectorReportResponse:
    sec = (body.sector or "").strip()
    if not sec:
        raise HTTPException(status_code=400, detail="sector 不能为空")
    y, q = current_report_cache_quarter()
    if not body.force_refresh:
        cached_md = get_cached_report(sec, y, q)
        if cached_md:
            return SectorReportResponse(
                sector=sec,
                year=y,
                quarter=q,
                report_md=cached_md,
                quarterly_data={},
                from_cache=True,
            )
    had_cache = False
    try:
        report_md, quarterly_data = generate_six_step_sector_report(
            sec,
            relevance_threshold=body.relevance_threshold,
            force_refresh=body.force_refresh,
        )
    except Exception as e:
        raise HTTPException(
            status_code=500,
            detail=f"生成行业报告失败：{type(e).__name__}: {e}",
        ) from e
    return SectorReportResponse(
        sector=sec,
        year=y,
        quarter=q,
        report_md=report_md,
        quarterly_data=quarterly_data,
        from_cache=had_cache,
    )
