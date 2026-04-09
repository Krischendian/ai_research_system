# AI 投研自动化系统（POC）

面向投研流程的 **Python 全栈概念验证**：从公开数据源抓取结构化与非结构化信息，经 **FastAPI** 暴露 REST API，由 **Streamlit** 提供看板。设计原则是 **可溯源**（披露原文、RSS 链接、段落 ID），LLM 仅做抽取与归纳，**不替代信源核对**。**本仓库产出仅供研究与工程联调，不构成投资建议。**

---

## 1. 架构总览

```text
┌─────────────────────────────────────────────────────────────┐
│  Streamlit（根目录 app.py → frontend/pages/）               │
│  HTTP → http://127.0.0.1:8000/api/v1/...                    │
└───────────────────────────┬─────────────────────────────────┘
                            │
┌───────────────────────────▼─────────────────────────────────┐
│  FastAPI（src/research_automation/main.py）                 │
│  api/v1/*  →  薄路由，HTTP 异常与业务错误映射                │
└───────────────────────────┬─────────────────────────────────┘
                            │
┌───────────────────────────▼─────────────────────────────────┐
│  services/        业务编排、Prompt、逐字校验、结果组装       │
│  extractors/      SEC 10-K/8-K、FMP、Benzinga、Finnhub、    │
│                   RSS、OpenAI、earningscall、sec-api.io 等   │
│  core/            SQLite、公司表、调度器、段落拆分与引用     │
│  models/          Pydantic 契约（与 OpenAPI 一致）           │
└───────────────────────────┬─────────────────────────────────┘
                            │
┌───────────────────────────▼─────────────────────────────────┐
│  data/research.db       财务行、公司、document_paragraphs     │
│  data/raw/              10-K 章节缓存、SEC 辅助、新闻洞察缓存  │
│  data/reports/          调度器落盘的隔夜/昨日总结等 JSON     │
└─────────────────────────────────────────────────────────────┘
```

**分层约定（扩展时请遵守）**

| 层级 | 职责 | 反例 |
|------|------|------|
| `api/v1/` | 解析参数、调用 service、抛出 `HTTPException` | 在路由里写长 Prompt 或直连 SQLite |
| `services/` | 业务流程、调用 LLM、合并多数据源 | 在 service 里裸抓 SEC（应放 `extractors/`） |
| `extractors/` | HTTP/文件/第三方 SDK，返回原始或弱结构化数据 | 依赖具体 API 路由 |
| `models/` | 数据结构，不写业务分支 | 在模型里调 `chat()` |
| `core/` | DB、调度、与领域无关的工具 | 耦合某一则 API 的 DTO |

**与 `backend/` 目录**：`backend/main.py` 仅为历史遗留的极简 FastAPI 样例（少量路由），**生产与本地联调请以 `src/research_automation/` 为准**（`uvicorn research_automation.main:app`）。新功能只改 `src/`，避免双轨漂移。

---

## 2. 技术栈

| 类别 | 技术 |
|------|------|
| 语言 | Python 3.10+（仓库内 `pyrightconfig.json` 标定 3.12 供类型检查） |
| API | FastAPI、Uvicorn、Pydantic v2 |
| 前端 | Streamlit 多页（`st.navigation`）、`requests`、**Plotly**（深度分析图表） |
| 数据库 | SQLite（`data/research.db`） |
| LLM | OpenAI Chat Completions（`extractors/llm_client.py`） |
| 财务（实时拉取逻辑） | **Financial Modeling Prep** `stable` 端点优先（`fmp_client`）；失败或无数据时 **SEC EDGAR 10-K Item 8**（`sec_edgar`） |
| 财务（REST 读库） | `GET /api/v1/companies/{ticker}/financials` **只读 SQLite**；入库依赖批量脚本 |
| 业务画像 | 10-K 多章节节选 + LLM；规则补全分部/地区占比 |
| 电话会逐字稿 | **FMP** → **EDGAR 8-K 附件**（`sec_8k_client`）→ **earningscall** → **sec-api.io**（需 `SEC_API_KEY`）；再经 LLM，段落溯源 |
| 公司新闻 | **Benzinga** 优先，`BENZINGA_API_KEY`；失败或为空时 **Finnhub** 兜底（`FINNHUB_API_KEY`） |
| 宏观新闻 | 多源 RSS（`news_client`），与公司源合并去重 |
| 晨报洞察 | `services/news_insights.py`：聚类、条目重要性、`analyst_briefing`（24h 磁盘缓存） |
| 调度 | APScheduler（随 FastAPI `lifespan` 启停） |
| 其它 | BeautifulSoup、lxml、pandas、python-dotenv、certifi、feedparser |

**依赖文件**

- `requirements.txt`：`-r backend/requirements.txt` + `-r frontend/requirements.txt`（前端含 **plotly**）。
- `requirements-dev.txt`：在上一项基础上增加 **pytest**。

---

## 3. API 功能清单

| 能力 | 说明 | 入口 |
|------|------|------|
| 财务序列 | **仅读** `data/research.db` 中已入库年度行；无数据时需先跑 `scripts/batch_fetch_financials.py`（脚本内 `get_financials()`：**优先 FMP**，否则 SEC） | `GET /api/v1/companies/{ticker}/financials` |
| 业务画像 | 10-K **多章节**合并节选 + LLM；分部/地区占比支持 Item 1 散文 % 与 Item 7/8 美元表推算；`key_quotes` 禁止来自 Item 1A 风险模板句 | `GET /api/v1/companies/{ticker}/business-profile` |
| 财报电话会 | 多源逐字稿 + LLM；`quotations` 须为逐字稿子串；`data_source`：`fmp` / `sec_8k` / `earningscall` / `sec_api` | `GET /api/v1/companies/{ticker}/earnings?quarter=2024Q4` |
| 晨报 | 宏观 + 公司新闻经 **两次 LLM**（摘要分区 + 聚类/评分/早评）；见下表「晨报 JSON」 | `GET /api/v1/news/morning-brief` |
| 隔夜速递 | 新闻时区窗口 + 摘要 + 条目（公司源 Benzinga/Finnhub + RSS） | `GET /api/v1/news/overnight` |
| 昨日总结 | 「昨日」全天 RSS + 公司源合并，主题归类 | `GET /api/v1/news/yesterday-summary` |
| 系统/调度 | 状态与手动触发（需环境变量） | `GET /api/v1/system/status`、`POST /api/v1/system/scheduler/trigger` |

**晨报响应要点（`models/news.py`）**

- `macro_news` / `company_news`：`NewsItem` 含 `title`、`summary`、`source`、`source_url`、`published_at`、`matched_tickers`、**`sentiment`**（`positive` \| `negative` \| `neutral`，摘要 LLM 输出）、**`importance_score`**（聚类后写回，1–10）。
- `clusters` / `top_news`：`ClusterNewsItem` 同样可带 **`sentiment`**（从对应简讯透传）。
- `analyst_briefing`、`data_source_label`、`provenance_note` 等见 OpenAPI。

**路径参数股票代码**：`financials` / `profiles` / `earnings` 均经 `core/ticker_normalize.normalize_equity_ticker`（例如 **APPL → AAPL**）。

---

## 4. 前端页面

| 页面 | 文件 | 说明 |
|------|------|------|
| 深度分析 | `frontend/pages/01_DeepDive.py` | **财务**：表格上方 **Plotly** 分面折线「财务趋势（最近三年）」；金额单位启发式（`\|x\|≥1e6` 视为美元÷1e9，否则视为百万美元÷1e3），避免 SEC 百万美元入库时被误÷1e9。**业务画像**：`revenue_by_segment` / `revenue_by_geography` 用 **环形饼图**（Plotly），分项溯源在 expander；**管理层展望**：`future_guidance` 为空或含「未明确提及」时友好提示 + **跳转到电话会议**（写 `st.session_state.deep_view_radio`）。**视图切换**：横向 `st.radio`（`key=deep_view_radio`）替代不可编程的 `st.tabs`，便于从展望区跳转。**电话会**、**📖** 段落、`deep_dive_prefill_ticker` / `deep_dive_auto_query` 与晨报联动。勿在带 `key` 的 `text_input` 实例化后改写同 key 的 `session_state`。 |
| 自动化晨报 | `frontend/pages/02_MorningBrief.py` | 分析师早评、今日必读、主题聚类、隔夜/昨日、宏观/公司新闻。**公司新闻**直接渲染 API 的 `company_news`（不与 `top_news` 按标题去重）。**主列表**：`importance_score` **≥4**（缺省按 **5**）；**≤3** 归入底部折叠 **「📦 背景资料（低分新闻）」**（无低分时整区不渲染）。标题条 **情绪底色**：优先 API **`sentiment`**，否则 `morning_brief_helpers.sentiment_from_text`。**深度分析**按钮对 `matched_tickers` **去重**，避免重复代码并排多钮。逻辑辅助见 `frontend/morning_brief_helpers.py`（含 `sentiment_for_item`、`sentiment_bg_color` 等）。 |

**入口**：项目根 `streamlit run app.py --server.port 8501`。默认后端 `http://127.0.0.1:8000`（`BACKEND_BASE`）；改端口时请同步修改各页。

---

## 5. SEC 10-K 与业务画像（实现要点）

- **`get_10k_sections(ticker, year)`**（`extractors/sec_edgar.py`）：`item1` / `item1a` / `item7` / `item8_notes`，缓存于 `data/raw/10k/{SYM}_{year}_sec_*.txt`。
- **画像合并顺序**：Item 1 → Item 7 → Item 8 附注 → Item 1A。
- **规则补全**：`revenue_by_segment` / `revenue_by_geography` 可由 Item 1 散文与 Item 7/8 表推算；FMP 分部可覆盖或校验 LLM 列表。

契约见 `models/company.py`（`BusinessProfile`、`SegmentMix`）与 `services/profile_service.py`。

---

## 6. 财报电话会与 8-K / sec-api

- **链路**（`services/earnings_service.py`）：**FMP** → **EDGAR 8-K**（`SEC_EDGAR_USER_AGENT`）→ **earningscall** → **sec-api.io**（可选 `SEC_API_KEY`）。
- **无稿**：**HTTP 503**。
- **模块**：`extractors/sec_8k_client.py`。

---

## 7. 财务：FMP、批量入库与 REST 的关系

| 环节 | 行为 |
|------|------|
| `financial_service.get_financials(ticker)` | 优先 `fmp_client`，否则 `sec_edgar.get_financial_statements` |
| `scripts/batch_fetch_financials.py` | 写入 SQLite；`--force` 覆盖；调度器或手动运行 |
| `GET .../financials` | 只读库；空表时提示跑 batch |

入库常见为 **百万美元** 量级整数（与 10-K 表脚注一致）；前端展示大额时已做 **十亿美元** 换算启发式，勿与 FMP 全美元混淆。

---

## 8. 限制与已知约束

1. **SQLite / 单进程**：POC 级。
2. **LLM 与 RSS**：摘要、情绪、聚类可能错漏；务必对照原文链接与段落。
3. **SEC**：`SEC_EDGAR_USER_AGENT`；遵守频率政策；部分 RSS **401** 常见。
4. **财务 REST**：须先 batch 入库。
5. **电话会**：依赖多源可用性与密钥层级。
6. **Benzinga / Finnhub**：皆无则公司新闻可能偏少。
7. **FMP**：无 key 时跳过 FMP 链路；逐字稿免费层常见 402。
8. **隔夜/昨日**：依赖时间戳与时间窗。
9. **Streamlit**：项目根运行；`--server.port`；`deep_view_radio` 等 key 勿与 widget 生命周期冲突。
10. **`.env`**：写入文件并重启进程，避免仅 `export` 与 API 进程不一致。

---

## 9. 环境与启动

### 9.1 要求

- Python **3.10+**
- 外网（OpenAI、SEC、RSS、可选 FMP / Benzinga / Finnhub / earningscall / sec-api）
- macOS / Linux（Windows 注意 `venv\Scripts\activate`）

### 9.2 安装依赖

```bash
cd /path/to/project
python3 -m venv venv
source venv/bin/activate
python -m pip install --upgrade pip
python -m pip install -r requirements.txt
python -m pip install -r requirements-dev.txt   # 测试
```

venv 问题可运行 `bash scripts/recreate_venv.sh`。

### 9.3 配置 `.env`

复制 `.env.example` 为 `.env`：

| 变量 | 说明 |
|------|------|
| `OPENAI_API_KEY` | 必填 |
| `SEC_EDGAR_USER_AGENT` | 强烈建议；10-K/8-K |
| `NEWS_TIMEZONE` | 默认 `America/New_York` |
| `BENZINGA_API_KEY` | 可选；公司新闻优先 |
| `FINNHUB_API_KEY` | 可选；兜底 |
| `FMP_API_KEY` | 可选；财务与电话会 FMP 源 |
| `SEC_API_KEY` | 可选；sec-api.io |
| `SCHEDULER_TIMEZONE` | 默认 `Asia/Shanghai` |
| `SCHEDULER_TEST_MODE` | `1` 时约 15s 跑一次隔夜+昨日 |
| `SCHEDULER_ENABLE_MANUAL_TRIGGER` | `1` 允许手动触发任务 |

### 9.4 启动后端

```bash
source venv/bin/activate
PYTHONPATH=src uvicorn research_automation.main:app --reload --port 8000
```

- 文档：`http://127.0.0.1:8000/docs` · 健康：`/health` · 调度：`/api/v1/system/status`

### 9.5 启动前端

```bash
source venv/bin/activate
streamlit run app.py --server.port 8501
```

### 9.6 准备数据（可选）

```bash
PYTHONPATH=src python scripts/batch_fetch_financials.py --ticker AAPL --force
```

监控池：`research_automation.core.company_manager`（如 `seed_default_tech_companies`）。

### 9.7 调度器

| 任务 | 默认 Cron（`SCHEDULER_TIMEZONE`，默认上海） | 说明 |
|------|---------------------------------------------|------|
| 财务批处理 | 每月 1 日 06:00 | `batch_fetch_financials` |
| 晨间报告 | 每日 06:30 | 隔夜 → 昨日总结 → `data/reports/` |

手动触发：`SCHEDULER_ENABLE_MANUAL_TRIGGER=1` 后 `POST /api/v1/system/scheduler/trigger`，body 如 `{"job":"reports"}`。`job`：`batch` \| `overnight` \| `daily` \| `reports`。

---

## 10. 目录结构（主路径）

```text
├── app.py                         # Streamlit 入口（st.navigation）
├── frontend/
│   ├── pages/
│   │   ├── 01_DeepDive.py         # 财务趋势图、饼图、画像、电话会、radio 切换
│   │   └── 02_MorningBrief.py     # 晨报、隔夜、昨日、低分折叠、情绪色
│   ├── morning_brief_helpers.py   # 情绪、NY 标签、财务摘要、ticker 页路径
│   └── streamlit_helpers.py
├── src/research_automation/
│   ├── main.py
│   ├── scheduler.py
│   ├── api/v1/
│   ├── services/                  # 含 news_service、news_insights、…
│   ├── extractors/
│   ├── core/
│   └── models/                    # news.py：NewsItem.sentiment 等
├── scripts/
│   ├── batch_fetch_financials.py
│   └── recreate_venv.sh
├── tests/
├── data/
│   ├── research.db
│   ├── raw/                       # 含 news_insights 缓存子目录等
│   └── reports/
├── backend/                       # 遗留样例，非主入口
├── requirements.txt
├── requirements-dev.txt
├── pyrightconfig.json
├── .env.example
├── README.md
└── project_plan.md
```

---

## 11. 测试

```bash
source venv/bin/activate
export PYTHONPATH=src
python -m pytest tests/ -q
```

- 离线 / mock：如 `tests/test_profiles.py`。
- 外网：如 `tests/test_sec_sections.py`。
- 密钥：`tests/test_llm.py`、`tests/test_sec_8k.py` 等。

---

## 12. 给扩展 / AI Agent 的提示

1. **契约优先**：改 `models/*.py` 时同步 OpenAPI 与 Streamlit 字段（如 `sentiment`、`importance_score`）。
2. **晨报**：摘要 Prompt 在 `news_service.py`；聚类/评分在 `news_insights.py`；新增字段需贯通 `news_items_to_flat_dicts` 与前端 `sentiment_for_item`。
3. **新数据源**：`extractors/` + `services/`，勿在 `api` 堆业务。
4. **段落溯源**：`normalize_paragraph_ref_list`、画像/电话会 `source_paragraphs`。
5. **Streamlit**：`text_input` / `radio` 的 `session_state` 与官方生命周期一致；深度分析用 `deep_view_radio` 切换子视图。
6. **CORS**：`main.py` 仅本地 Streamlit；生产收紧。

---

## 13. 文档与合规

- `project_plan.md` 与代码冲突时，**以本 README 与 `src/` 为准**。
- SEC：<https://www.sec.gov/os/accessing-edgar-data>

排查：**uvicorn 日志** → **`/docs` detail** → **`.env`** → **`research.db` + batch** → **电话会多源** → **晨报缓存**（`data/raw/news_insights/`）。
