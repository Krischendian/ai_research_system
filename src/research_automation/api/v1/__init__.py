"""API v1：产品级用例路由（行业报告、晨报）。"""
from fastapi import APIRouter

from research_automation.api.v1.morning_brief import router as morning_brief_router
from research_automation.api.v1.sector_reports import router as sector_reports_router

v1_router = APIRouter()
v1_router.include_router(sector_reports_router)
v1_router.include_router(morning_brief_router)
