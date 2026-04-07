# AI 投研自动化系统（POC）

面向投研流程的 **Python 全栈概念验证**：从公开数据源抓取结构化与非结构化信息，经 **FastAPI** 暴露 REST API，由 **Streamlit** 提供看板。设计原则是 **可溯源**（披露原文、RSS 链接、段落 ID），LLM 仅做抽取与归纳，**不替代信源核对**。**本仓库产出仅供研究与工程联调，不构成投资建议。**

---

## 1. 架构总览

```text
┌─────────────────────────────────────────────────────────────┐
│  Streamlit（frontend/、根目录 app.py）                        │
│  HTTP 调用 → http://127.0.0.1:8000/api/v1/...               │
└───────────────────────────┬─────────────────────────────────┘
                            │
┌───────────────────────────▼─────────────────────────────────┐
│  FastAPI（src/research_automation/main.py）                  │
│  api/v1/*  →  薄路由，异常映射为 HTTP                       │
└───────────────────────────┬─────────────────────────────────┘
                            │
┌───────────────────────────▼─────────────────────────────────┐
│  services/        业务编排、Prompt、结果校验与组装              │
│  extractors/      纯外部 IO：Yahoo、SEC、RSS、OpenAI          │
│  core/            SQLite、公司表、调度器、段落拆分工具          │
│  models/          Pydantic 契约（API 与对内一致）               │
└───────────────────────────┬─────────────────────────────────┘
                            │
┌───────────────────────────▼─────────────────────────────────┐
│  data/research.db      财务、公司、document_paragraphs        │
│  data/raw/             10-K 文本缓存、SEC 辅助 JSON 等          │
│  data/reports/         调度器落盘的隔夜/昨日总结 JSON          │
└─────────────────────────────────────────────────────────────┘
```

**分层约定（扩展时请遵守）**

| 层级 | 职责 | 反例 |
|------|------|------|
| `api/v1/` | 解析参数、调用 service、抛出 `HTTPException` | 在路由里写长 Prompt 或直连 SQLite |
| `services/` | 业务流程、调用 LLM、合并多数据源 | 在这里发裸 `requests` 抓 SEC（应放 `extractors/`） |
| `extractors/` | HTTP/文件/第三方 SDK，返回原始或弱结构化数据 | 依赖具体 API 路由 |
| `models/` | 数据结构，不写业务分支 | 在模型里调 `chat()` |
| `core/` | DB、调度、与领域无关的工具 | 耦合某一则 API 的 DTO |

**与旧目录 `backend/` 的关系**：仓库中仍保留历史布局 `backend/app/...`，**当前主实现以 `src/research_automation/` 为准**（`uvicorn research_automation.main:app`）。新功能请只改 `src/` 侧，避免双轨漂移。

---

## 2. 技术栈

| 类别 | 技术 |
|------|------|
| 语言 | Python 3.10+ |
| API | FastAPI、Uvicorn、Pydantic v2 |
| 前端 | Streamlit（多页：深度分析、晨报） |
| 数据库 | SQLite（`data/research.db`），财务读路径带进程内短缓存 |
| LLM | OpenAI Chat Completions（`extractors/llm_client.py`） |
| 财务 | yfinance → `financials` 表 |
| 披露 | SEC EDGAR（`extractors/sec_edgar.py`，须合规 User-Agent） |
| 新闻 | feedparser + requests（Reuters/Bloomberg/TechCrunch 等 RSS） |
| 调度 | APScheduler（`scheduler.py`，随 FastAPI `lifespan` 启停） |
| 其它 | BeautifulSoup、lxml、python-dotenv |

依赖安装：根目录 `requirements.txt` 聚合 `backend/requirements.txt` 与 `frontend/requirements.txt`。

---

## 3. 功能清单（便于对标扩展）

| 能力 | 说明 | 典型入口 |
|------|------|----------|
| 财务序列 | 多公司年度指标入库与查询 | `GET /api/v1/companies/{ticker}/financials` |
| 业务画像 | 10-K Item 1 分段入库 + LLM 抽取；**段落级溯源**（`field_paragraph_ids`、`source_paragraphs`） | `GET /api/v1/companies/{ticker}/business-profile` |
| 财报电话会 | Mock 逐字稿 + LLM；**段落溯源**；预留 Bloomberg | `GET /api/v1/companies/{ticker}/earnings?quarter=2024Q4` |
| 晨报 | RSS 聚合 + 宏观/公司分类 | `GET /api/v1/news/morning-brief` |
| 隔夜速递 | NY 时段窗口 + 一句摘要 + 条目列表 | `GET /api/v1/news/overnight` |
| 昨日总结 | NY「昨日」全天 RSS + 宏观/公司主题归类 Markdown | `GET /api/v1/news/yesterday-summary` |
| 系统/调度 | 调度状态、手动触发（需环境变量） | `GET /api/v1/system/status`、`POST /api/v1/system/scheduler/trigger` |
| 定时任务 | 月初 06:00 财务批处理；每日 06:30 隔夜+昨日报告落盘 `data/reports/` | 见下文「调度器」 |

**前端**

- **深度分析**（`frontend/pages/01_DeepDive.py`）：财务表、画像字段、管理层原话、公司动态、营收拆分；结论旁 **📖** 打开原文段落（依赖 `st.dialog` 或降级 expander）。
- **自动化晨报**（`frontend/pages/02_MorningBrief.py`）：隔夜速递、昨日总结（可折叠）、晨报正文。

---

## 4. 限制与已知约束（接手前必读）

1. **SQLite / 单进程**：POC 级；高并发或长时间任务需迁移 PostgreSQL 与任务队列，并Review 调度与 DB 锁。
2. **LLM 与 RSS**：摘要、分类、主题归并均可能错漏；**必须**通过 `source_url`、`primary_source_url`、`source_paragraphs` 回查。
3. **SEC**：须设置 `SEC_EDGAR_USER_AGENT`（见 `.env.example`）；频率与爬取策略受 SEC 政策约束，部分 RSS 返回 **401** 属常态，代码已做「单源失败不致命」处理。
4. **电话会**：当前为 **Mock 逐字稿**；接入付费源时在 `extractors/earnings_call.py` 扩展分支。
5. **隔夜/昨日新闻**：依赖 RSS **发布时间戳**；无时间戳的条目会被时间窗过滤掉。
6. **Streamlit**：子页通过 `from frontend.xxx` 导入；**须在项目根执行** `streamlit run ...`，且根目录 `app.py` 已将项目根加入 `sys.path`（见该文件）。Streamlit **端口** 使用 `--server.port`，不是 `--port`。
7. **环境变量与进程**：`export` 只对当前 shell 生效；**uvicorn 进程**需在启动前继承变量，或写入项目根 `.env`（调度器、`OPENAI_API_KEY` 等由 `python-dotenv` 加载）。

---

## 5. 环境与启动

### 5.1 环境要求

- Python **3.10+**
- 可访问外网（OpenAI、Yahoo、SEC、部分 RSS）
- macOS / Linux（Windows 注意路径与 `venv\Scripts\activate`）

### 5.2 安装

```bash
cd ai_research_system
python3 -m venv venv
source venv/bin/activate
pip install -r requirements.txt
```

### 5.3 配置 `.env`

复制 `.env.example` 为 `.env`，至少配置：

- `OPENAI_API_KEY`：真实密钥（勿提交 `.env`）。
- `SEC_EDGAR_USER_AGENT`：建议按 SEC 要求填写可识别应用及联系方式。
- 可选：`SCHEDULER_TIMEZONE`、`SCHEDULER_TEST_MODE`、`SCHEDULER_ENABLE_MANUAL_TRIGGER` 等（见 `.env.example`）。

### 5.4 启动后端

**必须在项目根目录**，并设置 `PYTHONPATH=src`：

```bash
source venv/bin/activate
PYTHONPATH=src uvicorn research_automation.main:app --reload --port 8000
```

- Swagger：`http://127.0.0.1:8000/docs`
- 健康检查：`http://127.0.0.1:8000/health`
- 系统状态：`http://127.0.0.1:8000/api/v1/system/status`

启动日志中应出现 **APScheduler 已启动**（若依赖已安装）。端口被占用时换 `--port 8001` 并同步改前端 `BACKEND_BASE`。

### 5.5 启动前端（Streamlit）

**推荐**：项目根目录使用多页导航入口（侧边栏 **深度分析 / 自动化晨报**）：

```bash
source venv/bin/activate
streamlit run app.py --server.port 8501
```

也可用 `frontend/app.py`（Streamlit 会自动识别 `frontend/pages/` 下页面），但根目录 `app.py` 与 `st.navigation` 的页标题、图标更完整。

前端默认请求 `http://127.0.0.1:8000`；若 API 端口变更，请改 `frontend/pages/01_DeepDive.py` 与 `02_MorningBrief.py` 中的 `BACKEND_BASE`。

### 5.6 准备示例数据（可选）

- 财务入库：`PYTHONPATH=src python tests/test_financials.py` 或 `PYTHONPATH=src python scripts/batch_fetch_financials.py`
- 监控池公司：见 `company_manager.seed_default_tech_companies` 等

### 5.7 调度器行为摘要

| 任务 | Cron（默认时区 `Asia/Shanghai`，可改 env） | 说明 |
|------|---------------------------------------------|------|
| 财务批处理 | 每月 1 日 06:00 | 调用 `scripts/batch_fetch_financials.run_batch_fetch` |
| 晨间报告 | 每日 06:30 | 顺序执行隔夜 + 昨日总结，写入 `data/reports/overnight_*.json`、`daily_*.json` |

测试：`SCHEDULER_TEST_MODE=1` 写入 `.env` 并重启 API，约 15 秒后各跑一次报告任务。手动触发需 `SCHEDULER_ENABLE_MANUAL_TRIGGER=1`，见 `POST /api/v1/system/scheduler/trigger`（body 如 `{"job":"reports"}`）。

---

## 6. 目录结构（主路径）

```text
ai_research_system/
├── app.py                      # Streamlit 推荐入口（navigation → frontend/pages）
├── frontend/
│   ├── app.py                  # 简易欢迎页（若单独 run 则与 pages 多页并存）
│   ├── pages/
│   │   ├── 01_DeepDive.py      # 深度分析
│   │   └── 02_MorningBrief.py  # 晨报 / 隔夜 / 昨日总结
│   └── streamlit_helpers.py
├── src/research_automation/    # ★ 主后端包
│   ├── main.py                 # FastAPI + lifespan（调度器）
│   ├── scheduler.py
│   ├── api/v1/                 # financials, profiles, earnings, news, system
│   ├── services/               # profile, news, overnight, daily_summary, earnings
│   ├── extractors/             # yahoo_finance, sec_edgar, news_client, llm_client, earnings_call
│   ├── core/                   # database, company_manager, paragraph_*
│   └── models/                 # financial, company, news, earnings, system
├── scripts/
│   └── batch_fetch_financials.py
├── tests/                      # unittest / 脚本式验证
├── data/
│   ├── research.db             # 运行后生成
│   ├── raw/                    # 10-K 缓存等（大类勿提交大体积时注意 .gitignore）
│   └── reports/                # 调度器 JSON
├── requirements.txt
├── .env.example
├── README.md
└── backend/                    # 历史/备用布局，非当前主入口
```

---

## 7. 自动化测试与调试

```bash
source venv/bin/activate
export PYTHONPATH=src

python -m unittest tests.test_financials tests.test_news_fallback tests.test_news_tickers tests.test_earnings -v
# LLM 连通性（需有效 OPENAI_API_KEY）
python tests/test_llm.py
```

新增模块后优先在 `tests/` 增加**不依赖外网**的单测，或对 LLM/RSS 使用 `unittest.mock`。

---

## 8. 给工程扩展 / AI Agent 的提示

1. **先读契约**：`models/*.py` 与 OpenAPI `/docs` 保持一致；改响应体时同步更新前端解析。
2. **新数据源**：新增 `extractors/foo.py`，在对应 `services/*` 拼接，**不要**在 `api` 里堆逻辑。
3. **新业务 API**：在 `api/v1/` 新建路由文件，于 `api/v1/__init__.py` 注册；保持前缀风格 `/api/v1/...`。
4. **段落溯源**：10-K / 电话会段落键名与 `document_paragraphs` 表结构在 `core/database.py`、`core/paragraph_text.py`；改 Prompt 时保持 **JSON 里 `p<number>` 或完整 `PARAGRAPH_ID`** 的约定，并在 service 层用 `paragraph_refs.normalize_paragraph_ref_list` 校验。
5. **调度新增任务**：改 `scheduler.py`，状态写入 `scheduler_last_runs.json`；必要时扩展 `GET /api/v1/system/status` 的模型。
6. **CORS**：当前允许本地 Streamlit 源；生产部署需收紧 `main.py` 中 `CORSMiddleware`。

---

## 9. 文档与合规

- 项目规划雏形见仓库内 `project_plan.md`（部分路径描述较早，**以 `src/research_automation` 实际结构为准**）。
- 使用 SEC 数据请阅读：https://www.sec.gov/os/accessing-edgar-data

如有异常：先看 **uvicorn 终端堆栈** 与 **`/docs` 返回的 `detail`**，再查 **SQLite 是否已 `init_db`**、**`.env` 是否被 API 进程加载**。
