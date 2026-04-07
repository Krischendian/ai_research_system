"""API v1 路由注册。"""
from fastapi import APIRouter

from research_automation.api.v1.earnings import router as earnings_router
from research_automation.api.v1.financials import router as financials_router
from research_automation.api.v1.news import router as news_router
from research_automation.api.v1.profiles import router as profiles_router
from research_automation.api.v1.system import router as system_router

v1_router = APIRouter()
v1_router.include_router(financials_router)
v1_router.include_router(profiles_router)
v1_router.include_router(earnings_router)
v1_router.include_router(news_router)
v1_router.include_router(system_router)
