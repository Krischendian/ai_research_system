# AI 投研自动化（POC）

基于 **Python** 的投研工程化项目：**FastAPI** 提供 `/api/v1` 数据与调度接口，**Streamlit** 提供操作界面。整合 FMP、SEC EDGAR、Benzinga/Finnhub 新闻、可选 Tavily 与 LLM，输出公司财务与画像、新闻简报、**行业六步报告**与执行摘要。

**仅用于研究与工程联调，不构成任何投资建议。**

---

## 1. 能力一览

| 能力 | 说明 | 典型入口 |
|------|------|----------|
| 公司财务 | 年报级指标（SQLite / FMP / SEC 回退链路） | `GET /api/v1/companies/{ticker}/financials` |
| 业务画像 | LLM 结构化业务摘要 | `GET /api/v1/companies/{ticker}/business-profile` |
| 电话会 | 逐字稿解析与要点（多数据源与缓存） | `GET /api/v1/companies/{ticker}/earnings` |
| 新闻简报 | 隔夜、昨日总结、晨报（RSS + LLM） | `GET /api/v1/news/*` |
| 搜索与问答 | 本地 `data/` 关键词检索 + 问答 | `POST /api/v1/search`、`/search/ask` |
| 行业报告 | Step0b～6 + 总览/卡片 + 执行摘要；前端可同进程调 service | `frontend/pages/04_SectorReport.py` |
| 调度 | APScheduler 生命周期；手动触发隔夜/昨日任务 | `GET/POST /api/v1/system/*` |

---

## 2. 架构与数据流

```text
Streamlit（根目录 app.py + frontend/pages/*）
        │
        ├─ HTTP → FastAPI（深度分析、新闻、搜索等页面）
        └─ 同进程 import → sector_report_service（行业报告页，可不经 HTTP）
                 │
                 ▼
FastAPI（src/research_automation/main.py，/api/v1/*）
                 │
                 ▼
services / extractors / core / models
                 │
                 ▼
SQLite（data/research.db、sector_report_cache、step_cache 等）
     + data/raw（10-K 片段、抓取缓存）+ data/reports（导出物）
```

**CORS**：`main.py` 默认放行 `http://localhost:8501` 与 `http://127.0.0.1:8501`。若 Streamlit 使用其他端口，需同步修改 `allow_origins`，否则浏览器跨域请求 API 会失败。

---

## 3. 代码布局（主路径）

- **`src/research_automation/`**：当前唯一主实现；`main.py` 为 ASGI 入口，`scheduler.py` 挂载调度任务。
- **`app.py`**：Streamlit 多页导航入口（见下文「前端页面状态」）。
- **`frontend/`**：页面脚本、`streamlit_helpers.py`、`morning_brief_helpers.py`；`frontend/requirements.txt` 由根 `requirements.txt` 引入。
- **`scripts/`**：批处理、行业报告 CLI、定时简报、种子数据、Notion 导出等。
- **`tests/`**：`pytest` 用例；`tests/services/test_post_generation_checker.py` 覆盖验证层 C。
- **`backend/`**：历史样例工程，**不是**日常运行主路径；根 `requirements.txt` 仍 `-r backend/requirements.txt` 以合并后端依赖声明。

`data/` 下含本地数据库与大量原始抓取文件，**不宜**在 README 中维护完整文件树；克隆后按需生成或通过脚本填充。

---

## 4. 前端页面状态（以 app.py 为准）

根目录 `st.navigation` 当前**默认仅注册**「行业监控报告」`frontend/pages/04_SectorReport.py`。下列页面文件仍存在，取消注释 `app.py` 中对应 `st.Page` 即可恢复侧栏入口：

- `01_DeepDive.py` — 深度分析  
- `02_MorningBrief.py` — 自动化晨报  
- `03_Search.py` — 全局搜索  
- `05_DailyBrief.py` — 日更简报相关  
- `06_SectorMatrix.py` — 板块矩阵类视图  

---

## 5. 行业报告流水线（核心）

实现集中在 **`src/research_automation/services/sector_report_service.py`**。

### 5.1 报告结构（逻辑顺序）

生成后的 Markdown 大致为：**标题元数据** → **板块总览** → **公司卡片** → **执行摘要** → **Step0b 公司快照表** → **Step2 收入拆分** → **Step3 展望** → **Step4 电话会** → **Step5 新业务 / 并购 / Insider** → **Step6 财务表与季度图**。

- **Step3 / Step4 / 执行摘要**：支持 **step 级缓存**（见 §7）；`force_refresh` 时跳过这些缓存。
- **Step5 / Step6**：当前实现中 **每次全量重算**（新闻与 Insider 需新鲜度）；Step5 含 **板块级 LLM 总结**（Benzinga + Form 4 语境），**不参与** `step_cache` 的 Step5 键（避免旧 Step5 整块报告缓存与续写策略不一致）。
- **整份报告缓存**：成功生成后写入 `sector_report_cache`；未 `force_refresh` 时可能直接返回缓存全文（因此若只改 Step5 逻辑，需清整报告缓存或强制刷新才能看到旧季度报告更新）。

### 5.2 三层验证（财务与文案）

1. **验证层 A（进 LLM 前）** — `_validate_and_sanitize_financials` / `_get_validated_financials`  
   - EBITDA 与净利润关系、净利率阈值、营收缺失时的联动降级、毛利率波动等；软件类例外 ticker 见 `checker_config.py`（`SOFTWARE_EXCEPTIONS` 等）。
2. **验证层 B（LLM 后数值）** — 分部金额、表内求和一致性等（如 `calculate_segment_dollars` 一类逻辑）。
3. **验证层 C（生成后检查）** — `services/post_generation_checker.py`  
   - 保留抽取、归因、比对与统计；**不向正文写入**内联标记（如 `[🔵]` / `[⚠️]` / 删除线批注）。`findings` / `summary` 供日志与调试。

### 5.3 执行摘要与排名表

- 执行摘要由 `_executive_summary` 驱动，模板化分块约束文案结构。
- **「相对强弱」排名表**由代码固定公司行（有营收等财务数据的标的），LLM 以 JSON 补全优势/风险列；解析失败时降级为空表占位，不阻断整份报告。
- 使用较高 `max_tokens` 并带截断续写，减少半成品输出。

### 5.4 Step2 / Step5 要点

- **Step2**：地理或分部收入展示前，与已校验总营收做 **覆盖率** 核对；过低覆盖会弱化或跳过易误导的地理表表述。
- **Step5**：Prompt 侧强调 **监控名单内 ticker**；输出侧 `_filter_step5_sector_signal` 做白名单过滤：整行仅非法标的时删除；合法与非法混排时对非法 `[TICKER]` / 词边界 ticker **脱敏为** `〔非本板块标的〕`。续写多轮与较高 token 上限减轻截断。  
  另对 LLM 偶发把 `[TICKER]` 拆成多行的情况，在过滤前用 **`_sanitize_step5_bracket_tickers`** 归并为合法括号形式。

### 5.5 调试辅助

- `_step5_new_biz_acquisitions_insider(..., sector_summary_only=True)` 可在加载 `per_company` 后**仅生成板块总结块**（含免责声明），便于 CLI 单独调试 Step5，而不输出参考来源与各公司 `###` 长节。

---

## 6. API 参考（`/api/v1`）

前缀均为 **`/api/v1`**（再叠加各 router 的 `prefix`）。

| 方法 | 路径 | 说明 |
|------|------|------|
| GET | `/companies/{ticker}/financials` | 公司年报财务 |
| GET | `/companies/{ticker}/business-profile` | 业务画像 |
| GET | `/companies/{ticker}/earnings` | 电话会分析（可带 `quarter` 等查询参数，见路由实现） |
| GET | `/news/overnight` | 隔夜新闻要点 |
| GET | `/news/yesterday-summary` | 昨日总结 |
| GET | `/news/morning-brief` | 晨报 |
| POST | `/search` | 关键词搜索 |
| POST | `/search/ask` | 问答 |
| GET | `/system/status` | 调度器与各任务上次运行信息 |
| POST | `/system/scheduler/trigger` | 手动触发调度任务 |

顶层：

- `GET /health` — 健康检查  
- `GET /` — 服务说明与 docs 入口  
- `GET /hello` — 简单连通性  

交互式文档：`http://127.0.0.1:8000/docs`（启动 uvicorn 后）。

---

## 7. 缓存与并发

| 类型 | 模块 / 表 | 作用 |
|------|-----------|------|
| 整份行业报告 | `services/report_cache.py` → `sector_report_cache` | 按 `(sector, year, quarter)` 存完整 Markdown；命中则跳过后续 LLM |
| Step 片段 | `core/database.py` → `step_cache` | 如 `step3`、`step4`、`exec_summary`；维度含 sector、年、季、版本号 |
| 电话会 / 画像等 | 各 service | 多复用 `get_step_cache` / `set_step_cache` |

**并发**：同 sector 报告生成带 **in-flight 锁**，避免重复大模型调用。

**清理示例**：

```bash
# 仅清某 sector 的执行摘要 step（year/quarter 可按需补上以收窄范围）
PYTHONPATH=src python -c "
from research_automation.core.database import clear_step_cache
n = clear_step_cache('AI_Job_Replacement', step='exec_summary')
print('deleted rows:', n)
"

# 删除整份报告缓存（与前端「强制刷新」一致思路）
PYTHONPATH=src python -c "
from research_automation.services.report_cache import delete_report_cache
delete_report_cache('AI_Job_Replacement', 2026, 1)
print('sector report cache deleted')
"
```

修改 `.env` 后请重启 **uvicorn** 与 **streamlit**。

---

## 8. 环境变量

复制模板并编辑：

```bash
cp .env.example .env
```

| 变量 | 用途 |
|------|------|
| `OPENAI_API_KEY` | OpenAI 路径（`llm_client` 在 Anthropic 无效或缺失时回退） |
| `ANTHROPIC_API_KEY` / `ANTHROPIC_MODEL` | 优先 Claude；需安装 **`anthropic`** 包（见 §9） |
| `SEC_EDGAR_USER_AGENT` | SEC 访问规范要求（含联系邮箱）；8-K / 10-K 链路必需 |
| `SEC_API_KEY` | 可选，sec-api.io 全文检索等回退 |
| `FMP_API_KEY` | 财务与电话会等（密钥需匹配 FMP 当前 API 前缀策略） |
| `BENZINGA_API_KEY` / `FINNHUB_API_KEY` | 公司新闻；优先 Benzinga，失败或无结果时 Finnhub |
| `TAVILY_API_KEY` | 可选，信号类 POC |
| `NEWS_TIMEZONE` | 隔夜/昨日等时间窗（IANA，默认 `America/New_York`） |
| `REPORT_RELEVANCE_THRESHOLD` | 行业报告新闻 relevance 下限（0–3，默认 1） |
| `SCHEDULER_TIMEZONE` / `SCHEDULER_TEST_MODE` / `SCHEDULER_ENABLE_MANUAL_TRIGGER` | 调度行为（详见 `scheduler.py` 与 `.env.example` 注释） |

**Notion 导出**（`scripts/notion_export.py`）：使用环境变量 **`NOTION_API_TOKEN`**（勿提交到版本库）。

---

## 9. 依赖与本地环境

```bash
python3 -m venv venv
source venv/bin/activate   # Windows: venv\Scripts\activate
pip install -r requirements.txt
# 走 Claude 优先路径时：
pip install anthropic
```

开发依赖（可选）：

```bash
pip install -r requirements-dev.txt
```

根 `requirements.txt` 合并了 `backend/requirements.txt` 与 `frontend/requirements.txt`，并额外包含如 `json-repair` 等共用库。

---

## 10. 启动方式

**后端**（需 `PYTHONPATH=src` 以便 `import research_automation`）：

```bash
cd "/path/to/untitled folder"
source venv/bin/activate
PYTHONPATH=src uvicorn research_automation.main:app --reload --port 8000
```

**前端**：

```bash
streamlit run app.py --server.port 8501
```

访问：

- API 文档：`http://127.0.0.1:8000/docs`  
- Streamlit：`http://127.0.0.1:8501`  

---

## 11. 常用脚本与命令

```bash
# 导入烟测
PYTHONPATH=src python -c "from research_automation.main import app; print('ok')"

# 行业报告 CLI
PYTHONPATH=src python scripts/generate_sector_report.py

# 大块服务文件语法检查（改 sector_report 后建议跑）
python -m py_compile src/research_automation/services/sector_report_service.py

# 定时简报（cron 示例可写 stderr 到 logs/brief_cron.log）
PYTHONPATH=src python scripts/scheduled_brief.py

# Notion 导出（需 NOTION_API_TOKEN）
python scripts/notion_export.py
```

其他 `scripts/`：`batch_fetch_financials.py`、`seed_ai_job_replacement.py`、`test_tavily_signals.py`、`test_core_business_quality.py`、`recreate_venv.sh` 等。

---

## 12. 测试

```bash
source venv/bin/activate
PYTHONPATH=src pytest tests -q
```

验证层 C 单测：

```bash
PYTHONPATH=src pytest tests/services/test_post_generation_checker.py -q
```

仓库根目录若存在 `test_fixes.py` 等临时脚本，**不属于**正式 `tests/` 套件，运行前请自行确认副作用。

---

## 13. 目录结构（精简树）

仅列出**版本控制内常用源码与配置**；`data/raw/**` 体量巨大，此处不展开。

```text
.
├── .env.example
├── .gitignore
├── app.py                      # Streamlit 入口
├── README.md
├── pyrightconfig.json
├── requirements.txt            # -r backend + frontend + 共用
├── requirements-dev.txt
├── daily_brief_cache.db        # 若存在：本地简报缓存（视运行而定）
├── logs/                       # 可选：如 brief_cron.log
├── data/
│   ├── .gitkeep
│   ├── research.db             # 主库（路径以 database 配置为准）
│   ├── reports/                # 导出报告、调度 JSON 等
│   ├── raw/                    # 10-K 片段、抓取缓存等
│   └── cache/                  # 部分宏观/新闻缓存 JSON
├── frontend/
│   ├── streamlit_helpers.py
│   ├── morning_brief_helpers.py
│   ├── requirements.txt
│   └── pages/
│       ├── 01_DeepDive.py
│       ├── 02_MorningBrief.py
│       ├── 03_Search.py
│       ├── 04_SectorReport.py
│       ├── 05_DailyBrief.py
│       └── 06_SectorMatrix.py
├── scripts/
│   ├── generate_sector_report.py
│   ├── scheduled_brief.py
│   ├── batch_fetch_financials.py
│   ├── seed_ai_job_replacement.py
│   ├── notion_export.py
│   ├── test_tavily_signals.py
│   ├── test_core_business_quality.py
│   └── recreate_venv.sh
├── src/research_automation/
│   ├── main.py
│   ├── scheduler.py
│   ├── api/v1/                 # financials, profiles, earnings, news, search, system
│   ├── core/                   # database, company_manager, sector_config, …
│   ├── extractors/             # fmp, sec, benzinga, llm_client, …
│   ├── models/
│   └── services/               # sector_report_service, report_cache, post_generation_checker, …
├── tests/
│   ├── fixtures/
│   └── *.py                    # 见 pytest 收集结果
└── backend/                    # 历史样例（非主运行路径）
    └── …
```

若 `.vscode/settings.json` 等存在，为编辑器本地配置，按需纳入版本库。

---

## 14. 常见问题

- **ImportError / 模块找不到**：确认当前 shell 已 `export PYTHONPATH=src`（或命令前缀带上）。  
- **行业报告内容「不更新」**：先区分是 **整报告缓存** 还是 **step 缓存**；改 Step5 逻辑后若仍见旧文，尝试 `delete_report_cache` 或前端强制刷新。  
- **执行摘要或排名异常**：查日志中 JSON 解析与降级分支；排名表有空单元格属兜底行为。  
- **前端调 API 失败**：核对 Streamlit 端口是否在 FastAPI CORS 白名单内。  
- **SEC / FMP 403、402**：检查 User-Agent、密钥权限与 FMP URL 版本是否与客户端一致。  
- **文档与代码不一致**：以 `src/research_automation/` 为准。

---

## 15. 合规与外链

- SEC 数据访问规范：<https://www.sec.gov/os/accessing-edgar-data>  
- 本项目仅用于研究与工程验证，**不构成投资建议**。
