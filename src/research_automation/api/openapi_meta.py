"""OpenAPI / Swagger 元数据（仅暴露 4 个产品 API）。"""

from __future__ import annotations

API_TITLE = "Research Automation API"
API_VERSION = "2.0.0"
API_DESCRIPTION = """
投研自动化 **产品 API**（共 **4** 个接口，见下方分组）。

### 1. 自动化晨报

| 方法 | 路径 | 说明 |
|------|------|------|
| `GET` | `/api/v1/morning-brief` | 隔夜速递 + 昨日总结（可选经典晨报块） |

### 2. 行业六步报告

| 方法 | 路径 | 说明 |
|------|------|------|
| `GET` | `/api/v1/sector-reports/sectors` | 活跃板块列表（下拉框） |
| `GET` | `/api/v1/sector-reports/{sector}` | 读取缓存报告；无缓存返回 404 |
| `POST` | `/api/v1/sector-reports` | 生成报告（同步，约 15–20 分钟） |

`GET /health` 等运维接口不在本 Swagger 中展示。

仅用于研究与工程验证，不构成投资建议。
""".strip()

# Swagger 左侧分组顺序
OPENAPI_TAGS: list[dict[str, str]] = [
    {
        "name": "morning-brief",
        "description": "① 自动化晨报 — `GET /api/v1/morning-brief`",
    },
    {
        "name": "sector-reports",
        "description": "② 行业报告 — `GET /sectors`、`GET /{sector}`、`POST /sector-reports`",
    },
]

# 仅出现在 Swagger 的 4 个 path（测试与 custom openapi 校验用）
PUBLIC_API_PATHS: frozenset[str] = frozenset({
    "/api/v1/morning-brief",
    "/api/v1/sector-reports",
    "/api/v1/sector-reports/sectors",
    "/api/v1/sector-reports/{sector}",
})

COMMON_ERROR_RESPONSES: dict[int, dict] = {
    400: {
        "description": "请求参数无效",
        "content": {
            "application/json": {
                "example": {"detail": "sector 不能为空"},
            }
        },
    },
    404: {
        "description": "无缓存报告",
        "content": {
            "application/json": {
                "example": {"detail": "无缓存报告：AI_Job_Replacement 2026Q1，请 POST 生成"},
            }
        },
    },
    500: {
        "description": "服务端未预期错误",
        "content": {
            "application/json": {
                "example": {"detail": "生成行业报告失败：RuntimeError: ..."},
            }
        },
    },
    503: {
        "description": "上游数据或 LLM 不可用",
        "content": {
            "application/json": {
                "example": {"detail": "新闻简报生成失败"},
            }
        },
    },
}
