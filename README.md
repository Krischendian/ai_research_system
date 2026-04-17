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
│  services/        业务编排、Prompt、逐字校验、全局搜索/RAG   │
│  extractors/      SEC 10-K/8-K、FMP、Benzinga、Finnhub、    │
│                   RSS、OpenAI、earningscall、sec-api.io 等   │
│  core/            SQLite、公司表、调度器、段落拆分与引用     │
│  models/          Pydantic 契约（与 OpenAPI 一致）           │
└───────────────────────────┬─────────────────────────────────┘
                            │
┌───────────────────────────▼─────────────────────────────────┐
│  data/research.db       财务行、公司、document_paragraphs     │
│  data/raw/              10-K 章节缓存、电话会逐字稿缓存、     │
│                         SEC 辅助、新闻洞察缓存等               │
│  data/reports/          隔夜/昨日 JSON、晨报洞察缓存、行业报告 *.md 等 │
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
| LLM | OpenAI Chat Completions（`extractors/llm_client.py`）；行业电话会横评 / 新闻简报等可优先 **Anthropic**（`services/earnings_sector_service.py`、`news_briefing_service.py`，需 `pip install anthropic` 且配置 `ANTHROPIC_API_KEY`） |
| 财务（实时拉取逻辑） | **Financial Modeling Prep** `stable` 端点优先（`fmp_client`）；失败或无数据时 **SEC EDGAR 10-K Item 8**（`sec_edgar`） |
| 财务（REST 读库） | `GET /api/v1/companies/{ticker}/financials` **只读 SQLite**；入库依赖批量脚本 |
| 业务画像 | 10-K 多章节节选 + LLM；规则补全分部/地区占比 |
| 电话会逐字稿 | **FMP** → **EDGAR 8-K 附件**（`sec_8k_client`）→ **earningscall** → **sec-api.io**（需 `SEC_API_KEY`）；再经 LLM，段落溯源 |
| 公司新闻（晨报/隔夜/昨日） | **Massive 托管 Benzinga v2**（`extractors/benzinga_news_client.py`，`BENZINGA_API_KEY` / `apiKey`）；失败或为空时 **Finnhub**（`FINNHUB_API_KEY`） |
| 行业报告新闻信号 | `SIGNAL_NEWS_PROVIDER`：`auto`（Benzinga 优先，过滤后无条再 **Tavily**）/ `benzinga` / `tavily`；`TAVILY_API_KEY` |
| 宏观新闻 | 多源 RSS（`news_client`），与公司源合并去重 |
| 晨报洞察 | `services/news_insights.py`：聚类、条目重要性、`analyst_briefing`（24h 磁盘缓存） |
| 调度 | APScheduler（随 FastAPI `lifespan` 启停） |
| 行业报告 Markdown | `services/sector_report_service.py`：池内新闻信号（`signal_fetcher`）+ 新闻简报 LLM（`news_briefing_service`，优先 **Anthropic**，否则 OpenAI）+ FMP 内部交易；可选电话会横评块（`earnings_sector_service`） |
| 其它 | BeautifulSoup、lxml、pandas、python-dotenv、certifi、feedparser；后端可选 **anthropic**（`backend/requirements.txt`） |

**依赖文件**

- `requirements.txt`：`-r backend/requirements.txt` + `-r frontend/requirements.txt`（前端含 **plotly**）。
- `requirements-dev.txt`：在上一项基础上增加 **pytest**。

---

## 3. API 功能清单

| 能力 | 说明 | 入口 |
|------|------|------|
| 财务序列 | **仅读** `data/research.db` 中已入库年度行；无数据时需先跑 `scripts/batch_fetch_financials.py`（脚本内 `get_financials()`：**优先 FMP**，否则 SEC） | `GET /api/v1/companies/{ticker}/financials` |
| 业务画像 | 10-K **多章节**合并节选 + LLM；`industry_view_text` / `industry_view_source`（摘录溯源）；分部/地区占比支持 Item 1 散文 % 与 Item 7/8 美元表推算；`key_quotes` 禁止来自 Item 1A 风险模板句；**合并**同标的 **Q4 电话会** `quotations` 入 `key_quotes`（`data_source=earnings_call`、`source_url`→深度分析）；**合并**近窗 **公司新闻** 入 `corporate_actions`（须含外链 `source_url`，关键词归类） | `GET /api/v1/companies/{ticker}/business-profile` |
| 财报电话会 | 多源逐字稿 + LLM；`quotations` 须为逐字稿子串；`data_source`：`fmp` / `sec_8k` / `earningscall` / `sec_api` | `GET /api/v1/companies/{ticker}/earnings?quarter=2024Q4` |
| 晨报 | 宏观 + 公司新闻经 **两次 LLM**（摘要分区 + 聚类/评分/早评）；见下表「晨报 JSON」 | `GET /api/v1/news/morning-brief` |
| 隔夜速递 | 新闻时区窗口 + 摘要 + 条目（公司源 Benzinga/Finnhub + RSS） | `GET /api/v1/news/overnight` |
| 昨日总结 | 「昨日」全天 RSS + 公司源合并，主题归类 | `GET /api/v1/news/yesterday-summary` |
| 系统/调度 | 状态与手动触发（需环境变量） | `GET /api/v1/system/status`、`POST /api/v1/system/scheduler/trigger` |
| 全局关键词搜索 | 子串检索（大小写不敏感）：`data/raw/10k/*_sec_*.txt`、可选 `data/reports/morning_brief_*.json`、监控池标的之电话会逐字稿（`data/raw/earnings_transcripts/` 或 **earningscall** 拉取缓存） | `POST /api/v1/search`，body：`{"query","limit"}`（`limit` 默认 20） |
| AI 问答（RAG） | 将问题扩展为中英文检索词后合并命中，取 top 片段 + **gpt-4o-mini** 作答；无命中时返回说明文案 | `POST /api/v1/search/ask`，body：`{"question"}` → `answer` + `sources` |
| 行业监控报告（Markdown） | 由 `generate_sector_report` 在 **Python/Streamlit** 侧组装字符串；**无** 独立 REST 路由 | 见 `frontend/pages/04_SectorReport.py`、`scripts/generate_sector_report.py` 与 **§4.2** |

**晨报响应要点（`models/news.py`）**

- `macro_news` / `company_news`：`NewsItem` 含 `title`、`summary`、`source`、`source_url`、`published_at`、`matched_tickers`、**`sentiment`**（`positive` \| `negative` \| `neutral`，摘要 LLM 输出）、**`importance_score`**（聚类后写回，1–10）。
- `clusters` / `top_news`：`ClusterNewsItem` 同样可带 **`sentiment`**（从对应简讯透传）。
- `analyst_briefing`、`data_source_label`、`provenance_note` 等见 OpenAPI。

**路径参数股票代码**：`financials` / `profiles` / `earnings` 均经 `core/ticker_normalize.normalize_equity_ticker`（例如 **APPL → AAPL**）。

---

## 4. 前端页面

| 页面 | 文件 | 说明 |
|------|------|------|
| 深度分析 | `frontend/pages/01_DeepDive.py` | **财务**：表格上方 **Plotly** 分面折线「财务趋势（最近三年）」；**表格上方**注明 **「单位：百万美元」**；纵轴金额启发式（`\|x\|≥1e6` 视为美元÷1e9，否则视为百万美元÷1e3）。**业务画像**：饼图与展望区同前；**行业判断**：`industry_view_source` 与段落 ID 用 **📖** 打开 **dialog**（摘录 + 10-K 段落）；**关键原话**：10-K 用段落弹窗，电话会条目 **`source_url`** 无逐字稿正文时用 **📖** 直链深度分析；**近期动态**：10-K 用段落 **📖**，新闻用 **📖** 新开 `source_url`。**视图切换**：`deep_view_radio`；`deep_dive_prefill_*`、`query_params`（`ticker`、`quarter`）与晨报/搜索联动。勿在带 `key` 的 `text_input` 实例化后改写同 key 的 `session_state`。 |
| 自动化晨报 | `frontend/pages/02_MorningBrief.py` | 分析师早评、今日必读、主题聚类、隔夜/昨日、宏观/公司新闻。**公司新闻**直接渲染 API 的 `company_news`。**主列表**：`importance_score` **≥4**（缺省 **5**）；**≤3** 归入 **「📦 背景资料（低分新闻）」**。情绪底色、深度分析去重等同前。为减少刷屏，**不展示** API 返回的长 **`data_source_label`**（内含 Benzinga/Finnhub 等配置说明）；**`provenance_note`** 仍以提示框展示（摘要须核对原文等）。逻辑见 `frontend/morning_brief_helpers.py`。 |
| 全局搜索 | `frontend/pages/03_Search.py` | 侧边栏 **「全局搜索」**。`st.radio`：**关键词搜索**（`POST /api/v1/search`）与 **AI 问答**（`POST /api/v1/search/ask`）。结果带来源标签与「查看原文」：10-K 链 SEC、`source_url`；新闻外链；电话会写 `deep_dive_prefill_ticker` / `deep_dive_prefill_quarter` 并 `switch_page` 至深度分析。 |
| 行业监控报告 | `frontend/pages/04_SectorReport.py` | 从 `companies` 表读取 **活跃** 标的的 **distinct sector**，调用 `generate_sector_report` 生成 Markdown（含新闻简报 LLM、行业概览、各公司内部交易、调试统计）；页面将 `src/` 加入 `sys.path` 并加载项目根 `.env`。可下载 `.md`。无 REST 暴露，逻辑全在 `services/`。 |

**入口**：项目根 `streamlit run app.py --server.port 8501`。默认后端 `http://127.0.0.1:8000`（各页 `BACKEND_BASE`）；改端口时请同步修改各页。

### 4.1 全局搜索与 RAG（实现要点）

- **服务**：`services/search_service.py` — `keyword_search`、`answer_question`；路由 `api/v1/search.py`。
- **关键词检索**：整段 `query` 作为子串匹配；英文问句会拆词 + 停用词过滤；中文公司名/主题会映射为英文词与 ticker（如「苹果」→ `Apple` / `AAPL`）。电话会摘要优先取开场白之后的命中，减轻 IR 欢迎语占满片段的问题。
- **晨报新闻入搜**：需存在 `data/reports/morning_brief_*.json`（按文件 mtime 取最新）；若未落盘则新闻侧无结果。
- **Streamlit 深度分析联动**：`01_DeepDive.py` 支持 URL 查询参数 `?ticker=&quarter=`（与上次签名变化时写入 session，避免覆盖用户已改输入），以及 `deep_dive_prefill_quarter` 与 `deep_dive_prefill_ticker` 对称。
- **可选环境变量**：`RESEARCH_STREAMLIT_ORIGIN`（默认 `http://127.0.0.1:8501`）— 后端生成的电话会跳转链接 origin；多页路径以当前 Streamlit 配置为准（常见 `/DeepDive?...`）。

### 4.2 行业监控报告（服务与脚本）

- **主入口（库）**：`services/sector_report_service.py` — `generate_sector_report(sector, ...)` 组装整份 Markdown；`generate_news_briefing_section` 可单独复用（传入 `_loaded` 避免重复拉取）。
- **新闻信号**：`services/signal_fetcher.py` — `fetch_signals_for_ticker`；UTC **最近 N 日历日**（`SECTOR_REPORT_NEWS_DAYS_BACK`，默认 7）与晨报「隔夜/昨日」（`NEWS_TIMEZONE` 美东短窗）**不是同一时间口径**。`compute_relevance_score` 用于 `relevance_score`（0–3），报告侧用 `REPORT_RELEVANCE_THRESHOLD`（默认 1）过滤。
- **全池 URL 去重**：同一 `url` 在整份 sector 报告中只计入 **先遍历到的 ticker**（`list_companies` 字母序），后续公司可能因此 **「本周期无已过滤新闻」** 仍属预期。
- **新闻简报 LLM**：`services/news_briefing_service.py`（`key_signals` 的 `confidence` 为 `high`/`medium`/`low`；原始条目的置信度启发式见 `signal_fetcher._compute_signal_confidence`）。
- **电话会横评 LLM**：`services/earnings_sector_service.py` — `build_earnings_cross_section_lines`；由调用方传入 `earnings_cross_review`（含 `companies_data` 或预计算 `lines`）。Streamlit 默认报告 **不含** 横评块，除非调用处注入（脚本 `scripts/rebuild_natural_gas_earnings_cross_from_report.py` 用于从既有 MD 解析 `companies_data` 后仅重跑横评段）。
- **脚本**：`scripts/generate_sector_report.py`（默认 `AI_Job_Replacement` sector）、`scripts/rebuild_natural_gas_earnings_cross_from_report.py`、`scripts/test_tavily_signals.py`、`scripts/test_news_briefing.py`、`scripts/test_news_section.py`、`scripts/test_earnings_section.py`、`scripts/seed_ai_job_replacement.py` 等。

---

## 5. SEC 10-K 与业务画像（实现要点）

- **`get_10k_sections(ticker, year)`**（`extractors/sec_edgar.py`）：`item1` / `item1a` / `item7` / `item8_notes`，缓存于 `data/raw/10k/{SYM}_{year}_sec_*.txt`。
- **画像合并顺序**：Item 1 → Item 7 → Item 8 附注 → Item 1A。
- **规则补全**：`revenue_by_segment` / `revenue_by_geography` 可由 Item 1 散文与 Item 7/8 表推算；FMP 分部可覆盖或校验 LLM 列表。
- **行业判断**：Prompt 输出 `industry_view_text`（中文归纳）与 `industry_view_source`（英文摘录/段落依据）；占位「原文未明确提及」时清空 `industry_view_source`。
- **电话会原话并入画像**：`analyze_earnings_call(symbol, filing_year, 4)` 的 `quotations` 追加到 `key_quotes`（`data_source=earnings_call`，共享 `source_url`=`RESEARCH_STREAMLIT_ORIGIN`/DeepDive）；`source_paragraphs` 合并电话会段落 ID。
- **新闻动态**：`get_company_news`（约 120 日窗）经关键词映射为 `corporate_actions`；**无有效 `link` 的新闻不写入**；`source_url` 必填。

契约见 `models/company.py`（`BusinessProfile`、`KeyManagementQuote`、`CorporateAction`、`SegmentMix`）与 `services/profile_service.py`。

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
6. **Benzinga / Finnhub**：皆无则晨报等公司新闻可能偏少；行业报告另可配 **Tavily**（`SIGNAL_NEWS_PROVIDER`、`TAVILY_API_KEY`）。
7. **FMP**：无 key 时跳过 FMP 链路；逐字稿免费层常见 402。
8. **隔夜/昨日**：依赖时间戳与时间窗。
9. **Streamlit**：项目根运行；`--server.port`；`deep_view_radio` 等 key 勿与 widget 生命周期冲突。
10. **`.env`**：写入文件并重启进程，避免仅 `export` 与 API 进程不一致。
11. **全局搜索 / RAG**：关键词与向量检索无关，长问句或中英混写依赖扩展词表；片段质量受本地缓存与 earningscall 可用性影响；RAG 依赖 `OPENAI_API_KEY`。
12. **行业报告**：池内无活跃公司或 **sector 全空** 时无法生成；Tavily 路径下 `relevance_score` 与关键词表偏「泛科技/裁员/内幕」语境，能源等传统行业稿件可能被阈值挡掉；调试区统计为 0 时优先查 **API Key 与 `SIGNAL_NEWS_PROVIDER`**。

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
| `OPENAI_API_KEY` | 必填（晨报/画像/电话会分析、**AI 问答 RAG**、新闻简报等） |
| `ANTHROPIC_API_KEY` | 可选；配置且已安装 `anthropic` 时，**新闻简报**与**电话会横评** LLM 优先走 Claude（`EARNINGS_SECTOR_MODEL` 等） |
| `EARNINGS_SECTOR_MODEL` / `EARNINGS_SECTOR_OPENAI_MODEL` | 可选；横评用模型覆盖（见 `.env.example`） |
| `SEC_EDGAR_USER_AGENT` | 强烈建议；10-K/8-K |
| `NEWS_TIMEZONE` | 默认 `America/New_York`（晨报隔夜/昨日；**非**行业报告 UTC 累加窗） |
| `BENZINGA_API_KEY` | 可选；Massive Benzinga v2（公司新闻 + 行业报告信号） |
| `FINNHUB_API_KEY` | 可选；公司新闻兜底 |
| `FMP_API_KEY` | 可选；财务、电话会 FMP 源、**行业报告内部交易** |
| `SEC_API_KEY` | 可选；sec-api.io |
| `SIGNAL_NEWS_PROVIDER` | 可选；`auto` / `benzinga` / `tavily`（行业报告新闻信号） |
| `TAVILY_API_KEY` | 可选；`auto`/`tavily` 时全网检索兜底 |
| `REPORT_RELEVANCE_THRESHOLD` | 可选；行业报告 `relevance_score` 下限 0–3，默认 1 |
| `SECTOR_REPORT_NEWS_DAYS_BACK` | 可选；行业报告 UTC 日历日窗长，默认 7 |
| `SCHEDULER_TIMEZONE` | 默认 `Asia/Shanghai` |
| `SCHEDULER_TEST_MODE` | `1` 时约 15s 跑一次隔夜+昨日 |
| `SCHEDULER_ENABLE_MANUAL_TRIGGER` | `1` 允许手动触发任务 |
| `RESEARCH_STREAMLIT_ORIGIN` | 可选；后端生成「电话会 → 深度分析」类链接时的 Streamlit 根地址，默认 `http://127.0.0.1:8501` |

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

行业报告 Markdown（脚本内默认 sector，见 `scripts/generate_sector_report.py`）：

```bash
PYTHONPATH=src python scripts/generate_sector_report.py
```

监控池：`research_automation.core.company_manager`（`seed_default_tech_companies()` 等）；行业示例：`scripts/seed_ai_job_replacement.py`。

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
│   │   ├── 01_DeepDive.py         # 财务表单位说明、趋势图、画像溯源 📖、电话会、query_params
│   │   ├── 02_MorningBrief.py     # 晨报（不展示长 data_source_label）、隔夜、低分折叠
│   │   ├── 03_Search.py           # 全局关键词搜索 + AI 问答（RAG）
│   │   └── 04_SectorReport.py     # 行业监控报告（generate_sector_report，可下载 MD）
│   ├── morning_brief_helpers.py   # 情绪、NY 标签、财务摘要、ticker 页路径
│   └── streamlit_helpers.py
├── src/research_automation/
│   ├── main.py
│   ├── scheduler.py
│   ├── api/v1/
│   ├── services/                  # profile、news_*、search_service、sector_report、signal_fetcher、insider、earnings_sector…
│   ├── extractors/
│   ├── core/
│   └── models/                    # news.py：NewsItem.sentiment 等
├── scripts/
│   ├── batch_fetch_financials.py
│   ├── generate_sector_report.py      # 默认 AI_Job_Replacement → data/reports/*.md
│   ├── rebuild_natural_gas_earnings_cross_from_report.py
│   ├── seed_ai_job_replacement.py
│   ├── test_earnings_section.py
│   ├── test_news_briefing.py
│   ├── test_news_section.py
│   ├── test_tavily_signals.py
│   └── recreate_venv.sh
├── tests/
├── data/
│   ├── research.db
│   ├── raw/                       # 10k/、earnings_transcripts/、news_insights/ 等
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
5. **业务画像合并**：电话会 `quotations`、新闻 `corporate_actions` 的字段形状见 `models/company.py`；改 `get_profile` 时保持 API 与前端契约（`data_source`、`source_url`、`industry_view_source`）。
6. **Streamlit**：`text_input` / `radio` 的 `session_state` 与官方生命周期一致；深度分析用 `deep_view_radio` 切换子视图。
7. **全局搜索**：新数据源放 `extractors/` + 在 `search_service` 中挂接；改 RAG Prompt 或检索策略时保持 `answer_question` 与 `keyword_search` 的 API 契约（`sources` 字段形状）。
8. **CORS**：`main.py` 仅本地 Streamlit；生产收紧。
9. **行业报告**：新过滤规则或信号源放 `signal_fetcher` / `extractors`；改 Markdown 章节结构时同步 `generate_sector_report` 与 Streamlit `04_SectorReport.py`；横评契约见 `generate_earnings_section` / `earnings_sector_service`。

---

## 13. 文档与合规

- `project_plan.md` 与代码冲突时，**以本 README 与 `src/` 为准**。
- SEC：<https://www.sec.gov/os/accessing-edgar-data>

排查：**uvicorn 日志** → **`/docs` detail** → **`.env`**（含 `OPENAI_API_KEY`、`BENZINGA_API_KEY` / `TAVILY_API_KEY`、`SIGNAL_NEWS_PROVIDER`）→ **`research.db` + batch** → **电话会多源** → **晨报缓存**（`data/raw/news_insights/`）→ **行业报告**（`SECTOR_REPORT_NEWS_DAYS_BACK`、`REPORT_RELEVANCE_THRESHOLD`、调试统计块）→ **搜索缓存**（`data/raw/10k/`、`data/raw/earnings_transcripts/`、`morning_brief_*.json`）。
