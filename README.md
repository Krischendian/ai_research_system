# AI 投研自动化（POC）

基于 Python 的投研工程化项目：后端 **FastAPI** + 前端 **Streamlit**，整合 FMP / SEC / 新闻源 / LLM，输出公司分析、新闻简报、行业六步报告与执行摘要。  
**仅用于研究和工程联调，不构成任何投资建议。**

---

## 1. 当前代码基线

- 主实现目录是 `src/research_automation/`
- API 入口是 `src/research_automation/main.py`
- 前端入口是根目录 `app.py`（多页面在 `frontend/pages/`）
- `backend/` 目录为历史样例，不是当前运行主路径
- `frontend/pages/04_SectorReport.py` 对行业报告是**同进程调用 service**，并非完全 HTTP-only 模式

---

## 2. 架构概览

```text
Streamlit (app.py + frontend/pages/*)
        │
        ├─ HTTP 调用 FastAPI（多数页面）
        └─ 直接调用 service（行业报告页）
                 │
                 ▼
FastAPI (src/research_automation/main.py, /api/v1/*)
                 │
                 ▼
services + extractors + core + models
                 │
                 ▼
SQLite (data/research.db) + data/raw + data/reports
```

---

## 3. 功能模块

- **深度分析**：财务、业务画像、电话会解析（`01_DeepDive.py`）
- **晨报/隔夜/昨日总结**：新闻抓取、情绪和重要性汇总（`02_MorningBrief.py`）
- **搜索 + 问答**：关键词检索和问答（`03_Search.py`, `services/search_service.py`）
- **行业六步报告**：Step0b/2/3/4/5/6 + 执行摘要 + 图表（`04_SectorReport.py`）
- **系统调度**：APScheduler 生命周期管理和手动触发（`api/v1/system.py`）

---

## 4. 行业报告（最新验证逻辑）

行业报告核心在 `services/sector_report_service.py`，当前有三层校验：

- **验证层 A（AI 前）**：`_validate_and_sanitize_financials` / `_get_validated_financials`
  - 典型规则：EBITDA vs Net Income、净利率阈值、Revenue 相关联动降级、毛利率波动与一致性检查
- **验证层 B（AI 后）**：分部金额计算与求和一致性（如 `calculate_segment_dollars` 等）
- **验证层 C（文案后置）**：`services/post_generation_checker.py`
  - 保留数字抽取/归因/比对/统计逻辑
  - **不再向报告正文写入任何内联标注**（如 `[🔵]` / `[⚠️]` / `~~...~~[🔴]`）
  - `findings` 和 `summary` 仍保留用于日志/调试

---

## 5. 缓存与并发策略

- **整份报告缓存**：`services/report_cache.py`
- **Step 级缓存**：`core/database.py` 的 `step_cache`
  - API：`get_step_cache` / `set_step_cache` / `clear_step_cache`
  - 维度：`(sector, year, quarter, step, ticker)`
- **电话会缓存**：`services/earnings_service.py`（复用 `step_cache`）
- **画像缓存**：`services/profile_service.py`（复用 `step_cache`）
- **并发保护**：同 sector in-flight 锁，避免重复 LLM 调用

---

## 6. API 概览（`/api/v1`）

- `GET /api/v1/companies/{ticker}/financials`
- `GET /api/v1/companies/{ticker}/business-profile`
- `GET /api/v1/companies/{ticker}/earnings?quarter=2024Q4`
- `GET /api/v1/news/morning-brief`
- `GET /api/v1/news/overnight`
- `GET /api/v1/news/yesterday-summary`
- `POST /api/v1/search`
- `POST /api/v1/search/ask`
- `GET /api/v1/system/status`
- `POST /api/v1/system/scheduler/trigger`

健康检查：
- `GET /health`
- `GET /`（返回 docs/status 入口）

---

## 7. 环境变量（`.env.example`）

建议复制：

```bash
cp .env.example .env
```

常用项：

- `OPENAI_API_KEY` / `ANTHROPIC_API_KEY`：LLM
- `SEC_EDGAR_USER_AGENT`：SEC 抓取必需
- `FMP_API_KEY`：财务与电话会
- `BENZINGA_API_KEY` / `FINNHUB_API_KEY`：新闻
- `TAVILY_API_KEY`：信号抓取
- `SEC_API_KEY`：sec-api.io 回退链路（可选）
- `REPORT_RELEVANCE_THRESHOLD`：行业报告新闻过滤阈值
- `NEWS_TIMEZONE`：新闻时间窗
- `SCHEDULER_TIMEZONE` / `SCHEDULER_TEST_MODE` / `SCHEDULER_ENABLE_MANUAL_TRIGGER`：调度

> 修改 `.env` 后请重启 `uvicorn` 和 `streamlit`。

---

## 8. 本地启动

```bash
python3 -m venv venv
source venv/bin/activate
pip install -r requirements.txt
```

启动后端：

```bash
PYTHONPATH=src uvicorn research_automation.main:app --reload --port 8000
```

启动前端：

```bash
streamlit run app.py --server.port 8501
```

访问地址：

- 后端 docs：`http://127.0.0.1:8000/docs`
- 前端：`http://127.0.0.1:8501`

---

## 9. 常用命令

快速导入烟测：

```bash
PYTHONPATH=src python3 -c "from research_automation.main import app; print('ok')"
```

生成行业报告（脚本）：

```bash
PYTHONPATH=src python3 scripts/generate_sector_report.py
```

---

## 10. 测试

全量测试：

```bash
source venv/bin/activate
PYTHONPATH=src pytest tests -q
```

验证层 C 测试（当前覆盖 `post_generation_checker`）：

```bash
pytest tests/services/test_post_generation_checker.py -q
```

---

## 11. 目录结构（完整文件树）

```text
.
├── .env.example
├── .gitignore
├── .vscode/
│   └── settings.json
├── app.py
├── README.md
├── project_plan.md
├── pyrightconfig.json
├── requirements-dev.txt
├── requirements.txt
├── daily_brief_cache.db
├── data/
│   ├── .gitkeep
│   ├── research.db
│   └── reports/
│       ├── ai_job_replacement_report_20260414.md
│       ├── daily_2026-04-06.json
│       ├── natural_gas_news_briefing_20260415_0257.md
│       ├── natural_gas_report_20260415.md
│       ├── overnight_2026-04-06.json
│       └── scheduler_last_runs.json
├── frontend/
│   ├── __init__.py
│   ├── app.py
│   ├── morning_brief_helpers.py
│   ├── requirements.txt
│   ├── streamlit_helpers.py
│   └── pages/
│       ├── 01_DeepDive.py
│       ├── 02_MorningBrief.py
│       ├── 03_Search.py
│       ├── 04_SectorReport.py
│       ├── 05_DailyBrief.py
│       └── 06_SectorMatrix.py
├── scripts/
│   ├── batch_fetch_financials.py
│   ├── generate_sector_report.py
│   ├── recreate_venv.sh
│   ├── scheduled_brief.py
│   ├── seed_ai_job_replacement.py
│   ├── test_core_business_quality.py
│   ├── test_tavily_signals.py
│   ├── notion_export.py
│   └── core_business_quality_last_run.log
├── src/research_automation/
│   ├── __init__.py
│   ├── main.py
│   ├── scheduler.py
│   ├── api/
│   │   ├── __init__.py
│   │   └── v1/
│   │       ├── __init__.py
│   │       ├── earnings.py
│   │       ├── financials.py
│   │       ├── news.py
│   │       ├── profiles.py
│   │       ├── search.py
│   │       └── system.py
│   ├── core/
│   │   ├── __init__.py
│   │   ├── company_manager.py
│   │   ├── database.py
│   │   ├── news_time.py
│   │   ├── paragraph_refs.py
│   │   ├── paragraph_text.py
│   │   ├── sector_config.py
│   │   ├── ticker_normalize.py
│   │   └── verbatim_match.py
│   ├── extractors/
│   │   ├── __init__.py
│   │   ├── benzinga_client.py
│   │   ├── bloomberg_rss_client.py
│   │   ├── earnings_call.py
│   │   ├── earningscall_lib.py
│   │   ├── finnhub_news.py
│   │   ├── fmp_client.py
│   │   ├── llm_client.py
│   │   ├── news_client.py
│   │   ├── sec_8k_client.py
│   │   ├── sec_edgar.py
│   │   └── tavily_client.py
│   ├── models/
│   │   ├── __init__.py
│   │   ├── company.py
│   │   ├── earnings.py
│   │   ├── financial.py
│   │   ├── news.py
│   │   └── system.py
│   └── services/
│       ├── __init__.py
│       ├── chart_service.py
│       ├── checker_config.py
│       ├── daily_brief_service.py
│       ├── daily_summary_service.py
│       ├── earnings_service.py
│       ├── financial_service.py
│       ├── hallucination_guard.py
│       ├── insider_service.py
│       ├── news_insights.py
│       ├── news_service.py
│       ├── overnight_service.py
│       ├── post_generation_checker.py
│       ├── profile_service.py
│       ├── report_cache.py
│       ├── search_service.py
│       ├── sector_report_service.py
│       └── signal_fetcher.py
├── tests/
│   ├── fixtures/
│   │   └── profile_prompt_excerpt.txt
│   ├── services/
│   │   └── test_post_generation_checker.py
│   ├── test_company_manager.py
│   ├── test_daily_summary.py
│   ├── test_e2e_mixed.py
│   ├── test_earnings.py
│   ├── test_earningscall.py
│   ├── test_financials.py
│   ├── test_finnhub.py
│   ├── test_fmp_earnings.py
│   ├── test_fmp_financials.py
│   ├── test_fmp_integration.py
│   ├── test_fmp_json_shapes.py
│   ├── test_llm.py
│   ├── test_news_fallback.py
│   ├── test_news_insights.py
│   ├── test_news_tickers.py
│   ├── test_overnight.py
│   ├── test_profiles.py
│   ├── test_sec_8k.py
│   ├── test_sec_financials.py
│   └── test_sec_sections.py
└── backend/  # 历史样例代码
    ├── requirements.txt
    ├── main.py
    └── app/
        ├── __init__.py
        ├── api/
        │   ├── __init__.py
        │   └── v1/
        │       ├── __init__.py
        │       ├── financials.py
        │       └── profiles.py
        ├── core/
        │   ├── __init__.py
        │   ├── config.py
        │   └── database.py
        ├── extractors/
        │   ├── __init__.py
        │   ├── llm_client.py
        │   └── yahoo_finance.py
        ├── models/
        │   ├── __init__.py
        │   ├── company.py
        │   └── financial.py
        └── services/
            ├── __init__.py
            ├── financial_service.py
            └── profile_service.py
```

---

## 12. 常见问题排查

- 后端启动失败先看 `ImportError` / 路由导入错误
- 终端命令要注意空格与路径（如 `cd "...folder" && uvicorn ...`）
- 行业报告很慢先看：`force_refresh`、step 缓存命中、是否并发同 sector
- 搜索结果为空先检查 `data/raw` 与 `data/reports` 是否有可检索内容
- 文档与实现冲突时，以 `src/` 代码为准

---

## 13. 合规声明

- SEC 访问规范：<https://www.sec.gov/os/accessing-edgar-data>
- 本项目仅用于研究和工程验证，不构成投资建议
