"""OpenAPI / Swagger 元数据（供 main.py 与路由复用）。"""

from __future__ import annotations

API_TITLE = "Research Automation API"
API_VERSION = "1.0.0"
API_DESCRIPTION = """
AI 投研自动化后端：公司财务、业务画像、电话会分析、新闻简报、本地检索与调度任务。

**交互文档**

- [Swagger UI](/docs) — 在线调试
- [ReDoc](/redoc) — 只读文档
- [OpenAPI JSON](/openapi.json) — 可导入 Postman / Insomnia

**业务路由前缀**：`/api/v1`

**数据回退（摘要）**

| 能力 | 优先级 |
|------|--------|
| 财务 | Bloomberg → FMP → SEC 核验 |
| 画像 | SEC 10-K/20-F + LLM → FMP profile |
| 电话会 | FMP → EDGAR 8-K → sec-api.io |

仅用于研究与工程验证，不构成投资建议。
""".strip()

OPENAPI_TAGS: list[dict[str, str]] = [
    {
        "name": "financials",
        "description": "公司年报级财务指标（Bloomberg / FMP 回退）。",
    },
    {
        "name": "profiles",
        "description": "业务画像：SEC 节选 + LLM 结构化抽取；失败时 503。",
    },
    {
        "name": "earnings",
        "description": "财报电话会逐字稿分析与结构化摘要；无稿时 503。",
    },
    {
        "name": "news",
        "description": "隔夜要点、昨日总结、自动化晨报（RSS + LLM）。",
    },
    {
        "name": "search",
        "description": "本地 `data/` 关键词检索与 RAG 简答。",
    },
    {
        "name": "health",
        "description": "服务健康检查与根信息。",
    },
]

# 路由层复用的标准错误响应（Swagger「Responses」面板）
COMMON_ERROR_RESPONSES: dict[int, dict] = {
    400: {
        "description": "请求参数无效（如 ticker 为空、quarter 格式错误）",
        "content": {
            "application/json": {
                "example": {"detail": "query 参数 quarter 格式须为 YYYYQN，例如 2024Q4"},
            }
        },
    },
    403: {
        "description": "功能未启用（如手动调度触发）",
        "content": {
            "application/json": {
                "example": {"detail": "手动触发未启用，请设置 SCHEDULER_ENABLE_MANUAL_TRIGGER=1"},
            }
        },
    },
    500: {
        "description": "服务端未预期错误",
        "content": {
            "application/json": {
                "example": {"detail": "读取财务数据时发生未预期错误：RuntimeError: ..."},
            }
        },
    },
    503: {
        "description": "上游数据或 LLM 不可用（如无逐字稿、画像生成失败）",
        "content": {
            "application/json": {
                "example": {"detail": "未找到可用逐字稿"},
            }
        },
    },
}
