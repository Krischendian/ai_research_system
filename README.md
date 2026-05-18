# AI 投研自动化

基于 **Python** 的投研工程化项目：**FastAPI** 暴露 `/api/v1` 数据与调度接口，**Streamlit** 提供操作界面。整合 **Bloomberg（可选 DigitalOcean PostgreSQL 只读库）**、FMP、SEC EDGAR、Benzinga/Finnhub 新闻、可选 **Bloomberg RSS**（头条订阅，与 Benzinga 并行写入信号）、可选 Tavily，以及 **Anthropic（Claude）/ OpenAI** LLM，输出公司财务与业务画像、电话会分析、新闻简报、**行业六步报告**及调度任务产物。行业报告内新闻类脚注与「参考来源」表通过 **`_news_src_label`** 按信号上的 `query_axis` 动态标注 Benzinga / Bloomberg，不再写死单一来源。

本文档按**当前仓库实现**整理（最近通篇对齐：**2026-05**）；与代码冲突时以 `src/research_automation/`、`.env.example`、`claude.md` 为准。

**仅用于研究与工程验证，不构成任何投资建议。**

---

## 1. 能力一览

| 能力 | 说明 | 典型入口 |
|------|------|----------|
| 公司财务 | 年报级指标：**Bloomberg（可关）→ FMP** 回退（SEC EDGAR 同年 XBRL 仅做双源核验）；`_get_validated_financials` 返回 `(rows, issues, source_label)` 三元组，`source_label ∈ {"Bloomberg", "FMP"}` 供下游动态标注 | `GET /api/v1/companies/{ticker}/financials` |
| 业务画像 | SEC 10-K/20-F 节选 + LLM；失败或质量差时 **FMP company profile** 回退；含 **`industry_view` 截断检测 + 续写**（最多 2 轮）、`future_guidance` 内容质量校验 | `GET /api/v1/companies/{ticker}/business-profile` |
| 电话会 | 逐字稿：**FMP** → EDGAR 8-K → sec-api.io + LLM；部分非美股 ticker 映射到 FMP symbol；**`analysis.quarter` 反映 FMP 实际命中季度**（与请求不同时打日志） | `GET /api/v1/companies/{ticker}/earnings?quarter=2024Q4` |
| 新闻简报 | 隔夜、昨日总结、晨报（RSS + LLM）；可带 `sector`；**通用词 ticker 噪音过滤**（`TGT`/`RTO`/`EL`/`DG` 需含实体级关键词） | `GET /api/v1/news/*` |
| 搜索与问答 | 本地 `data/` 关键词检索 + 简答 RAG | `POST /api/v1/search`、`POST /api/v1/search/ask` |
| 行业报告 | 板块概览 + 公司卡片 + **执行摘要**（多子块；前端按 `##` 切片 Tab）+ Step0b/2/3/4/5/6；**产品线分部 / 地理收入** 在连上 Bloomberg PG 时 **Bloomberg 优先**；**数据覆盖表 / 财务快照** 来源标签综合 `_get_validated_financials` 与画像 `data_source_label`（含 Bloomberg 字样时并入）；Step5 内部交易走 `get_insider_summary`，**引用块脚注**为固定简述（不再拼接每司回退链）；Step4/5 强制 bullet 以 `[TICKER]` 开头；执行摘要与 Step5 正文含 **`_strip_redundant_h2` / `_filter_non_sector_content` / `_dedup_theme_bullets`** 等后处理（见 §6） | `frontend/pages/04_SectorReport.py` |
| PDF 逐字稿注入 | 本地脚本将 **Bloomberg PDF** 文本写入 `step_cache`（`__earnings__` / `step4_analysis`），绕过拉取链路 | 根目录 `inject_transcripts.py` |

---

## 2. 架构与数据流

```text
┌─────────────────────────────────────────────────────────────────┐
│  Streamlit  app.py  +  frontend/pages/*.py                       │
│  · 行业报告页：直接 import generate_six_step_sector_report         │
│  · 晨报等页：HTTP → FastAPI（127.0.0.1:8000）                      │
│  · 所有承载 LLM 输出的 st.markdown 走 _safe_md() 转义 $（防 KaTeX）│
└───────────────────────────┬─────────────────────────────────────┘
                            │
                            ▼
┌─────────────────────────────────────────────────────────────────┐
│  FastAPI  research_automation.main:app  /api/v1/*                 │
│  CORS：localhost / 127.0.0.1 :8501、:8502（改端口需同步 main.py）   │
└───────────────────────────┬─────────────────────────────────────┘
                            │
        ┌───────────────────┼───────────────────┐
        ▼                   ▼                   ▼
  extractors/          services/            core/
  (fmp_client,        (sector_report,       (database,
   sec_edgar,         insider_service,       company_manager,
   bloomberg_reader,  profile_service,       sector_config,
   llm_client,        earnings_service,      paragraph_text,
   signal_fetcher,…)  chart_service, …)      ticker_normalize, …)
                            │
                            ▼
              SQLite  data/research.db
              · companies, step_cache, sector_report_cache,
                document_paragraphs, …
              data/raw/      抓取与 10-K 片段缓存
              data/reports/  JSON/Markdown 导出、调度输出
```

**LLM**（`extractors/llm_client.py`）

- 优先 **Anthropic**（`ANTHROPIC_API_KEY`），否则 **OpenAI**（`OPENAI_API_KEY`）。
- **`ANTHROPIC_MODEL`**：默认 Claude 模型；**板块业务构成分布**（Step 0）使用该模型（不传 `task="finance"`），便于用 Sonnet 控成本与稳定性（Opus 4.7 在中文长列表上偶发提前 `end_turn`）。参考值：`claude-sonnet-4-6`。
- **`ANTHROPIC_MODEL_FINANCE`**：`chat(..., task="finance")` 时使用，行业报告中其余金融向 LLM 步骤（Step3/4/5、执行摘要等）均走此模型；未设置则回落到 `ANTHROPIC_MODEL`。参考值：`claude-opus-4-7`。
- Claude 成功返回后在 **INFO** 日志中记录 `stop_reason` 与 `output_tokens`，便于排查 `max_tokens` 截断。

**Bloomberg 只读库**（`extractors/bloomberg_reader.py`）

- 在 **`DO_DB_*`** 配置完整且未设置 **`BLOOMBERG_DB_ENABLED=0`** 时连接 DigitalOcean PostgreSQL 的 **`bloomberg`** schema；ETL 在 Windows 端（`bbg_etl/`）单独运行，本仓库**只读**。
- `psycopg2` 惰性导入，未安装或未配库时仍可启动 API/Streamlit。
- 所有读取函数均接受展示用代码（如 **`IBM US Equity`**）→ 内部规范为 `internal_ticker`，并保留 **`DHL`→`DHL GY`** 等种子简码 fallback。

`bloomberg` schema 表与对应读取函数：

| 函数 | 来源表 | 说明 |
|------|--------|------|
| `get_security_info` | `securities` | 名称、交易所、币种、GICS |
| `get_financials_annual` / `get_financials_quarterly` | `financials_annual` / `financials_quarterly` | 标准化年报 / 季报（30 家 × 6 年 / 12 季） |
| `is_data_fresh` | `financials_annual.fetched_at` | 新鲜度判断 |
| `get_earnings_transcript` | `earnings_transcripts` | 仅库内已 ETL 的逐字稿（**当前 `earnings_service` 主路径仍是 FMP → EDGAR → sec-api**） |
| `get_geo_revenue` | `geo_revenue`（PG_REVENUE + GEO override） | `period_label` 为 `"FY 2025"` 等原始格式 |
| `get_segment_revenue` | `segment_revenue`（PG_REVENUE 默认） | revenue 单位为 **百万美元** |
| `get_insider_monthly` | `insider_monthly`（INSIDER_MONTHLY_TRANSACTION） | SQL 层已用 `ABS(...) < 1e-10` 过滤 Bloomberg 浮点 null 占位 |

行业报告与 `insider_service` 在各自入口**优先调用**上述函数（分部、地理、月度内部人），失败或无行时再走 FMP / SEC / 画像逻辑。

---

## 3. 仓库目录树（交接用）

仅列主要路径；`data/raw` 与 `bbg_etl/`（Windows 端 ETL，单独仓库或目录）不在此展开。

```text
.
├── .env.example                 # 环境变量模板（勿提交 .env）
├── .gitignore
├── .streamlit/config.toml       # Streamlit：关闭 fileWatcher 等
├── README.md
├── claude.md                    # 项目上下文 / Session 交接记录
├── app.py                       # Streamlit 入口
├── inject_transcripts.py        # 将本地 PDF 逐字稿注入 step_cache
├── test_bbg.py                  # 烟测：打印若干标的 _get_revenue_segments 来源
├── requirements.txt
├── requirements-dev.txt
├── pyrightconfig.json
│
├── data/
│   ├── research.db              # 主 SQLite
│   ├── reports/                 # 隔夜/日报 JSON、行业报告 md 等
│   ├── raw/                     # 10-K 缓存、8-K 逐字稿缓存等
│   └── cache/                   # 部分新闻/宏观缓存
├── logs/                        # 可选，cron 日志
│
├── frontend/
│   ├── requirements.txt         # streamlit, requests, plotly
│   ├── streamlit_helpers.py
│   ├── morning_brief_helpers.py
│   └── pages/
│       ├── 01_DeepDive.py
│       ├── 02_MorningBrief.py   # app.py 已注册
│       ├── 03_Search.py
│       ├── 04_SectorReport.py   # app.py 已注册；_safe_md() 转义 $
│       ├── 05_DailyBrief.py
│       └── 06_SectorMatrix.py
│
├── run_streamlit.sh             # 使用项目 venv 启动 Streamlit
│
├── src/research_automation/
│   ├── main.py                  # FastAPI + CORS
│   ├── api/v1/                  # financials / profiles / earnings / news / search / system
│   ├── core/                    # database, company_manager, ticker_normalize, sector_config, …
│   ├── extractors/              # fmp_client, sec_edgar, llm_client, bloomberg_reader, …
│   ├── models/
│   └── services/                # sector_report_service, insider_service,
│                                # profile_service, earnings_service, chart_service,
│                                # signal_fetcher, …
│
├── tests/
└── project_plan.md
```

---

## 4. HTTP API 参考

基址：`http://127.0.0.1:8000`。业务路由均在 **`/api/v1`** 下（另有根级 `/health`、`/`、`/hello`）。

| 方法 | 路径 | 说明 |
|------|------|------|
| GET | `/api/v1/companies/{ticker}/financials` | 年报财务（Bloomberg → FMP；Bloomberg 可关闭） |
| GET | `/api/v1/companies/{ticker}/business-profile` | 业务画像（503 = 生成失败） |
| GET | `/api/v1/companies/{ticker}/earnings?quarter=2024Q4` | 电话会分析；`quarter` 格式 `YYYYQN`（**返回 `analysis.quarter` 为 FMP 实际命中**，可能与请求不同）；503 = 无逐字稿 |
| GET | `/api/v1/news/overnight?sector=...` | 隔夜要点 |
| GET | `/api/v1/news/yesterday-summary?sector=...` | 昨日总结 |
| GET | `/api/v1/news/morning-brief` | 晨报 |
| POST | `/api/v1/search` | Body：`{ "query", "limit" }` 关键词搜索 |
| POST | `/api/v1/search/ask` | Body：`{ "question" }` 检索 + 简答 |

交互式文档：启动 uvicorn 后访问 **`/docs`**。

---

## 5. 前端（Streamlit）

根目录 **`app.py`** 当前注册：

- **行业监控报告** → `frontend/pages/04_SectorReport.py`
- **自动化晨报** → `frontend/pages/02_MorningBrief.py`

其他页面（DeepDive / Search / DailyBrief / SectorMatrix）仍在 `frontend/pages/`，可在 `app.py` 的 `st.navigation([...])` 中以 `st.Page(...)` 重新加入。

- **Plotly**：行业报告等使用 `st.plotly_chart`，依赖见 `frontend/requirements.txt`。
- 当前 Streamlit 使用 **`width="stretch"` / `width="content"`**（已替换弃用的 `use_container_width`）。
- **CORS**：晨报等页面通过浏览器请求后端时，Streamlit 源端口须落在 `main.py` 的 `allow_origins`（默认 **8501、8502**）。
- **配置**：`.streamlit/config.toml` 将 `fileWatcherType` 设为 `none`；改 `.py` 后请在浏览器中手动刷新。
- **`st.session_state` 内存缓存**：清完 SQLite 缓存后浏览器需 **Cmd+R / Ctrl+R**，否则 Streamlit 仍展示旧报告。
- **长时间任务**：行业报告 **10～20 分钟+**（`force_refresh=True` 更长）；界面多为 `st.spinner`。开发可设 `PYTHONUNBUFFERED=1` 或 `logging.basicConfig(level=logging.INFO)`。

### 5.1 KaTeX 与 `$` 渲染（必读）

Streamlit `st.markdown` **默认开启 KaTeX**，会把 `$...$` 解读为 LaTeX inline math，导致金额字符串（如「$2.2 billion」「$1.5M」）被逐字符渲染、空格散开。

**统一解决方案**：`frontend/pages/04_SectorReport.py` 在文件顶部定义 `_safe_md(text)`，把 `$` 转义为 `\$` 再传入 `st.markdown`。**所有承载 LLM 输出 / 财务数字的 markdown 调用都必须走 `_safe_md`**（已覆盖整页所有调用点）。

如新增 markdown 渲染入口（如 DeepDive、SectorMatrix），同样需要补 `_safe_md` 包装；切勿改 LLM prompt 让模型避开 `$`——LLM 输出的金融原文必然含美元符号，不应让模型回避，渲染层才是正确的修复位置。

---

## 6. 行业报告流水线

实现核心：**`src/research_automation/services/sector_report_service.py`**（约 **5200+** 行），入口 **`generate_six_step_sector_report(sec, *, force_refresh=False, ...)`**。

### 6.1 输出结构（Markdown 顺序）

标题元数据 → **板块概览**（板块业务构成分布 + 数据覆盖表）→ 公司卡片（H-11）→ **执行摘要**（子块正文由 LLM 生成；**勿**在子块内再输出 `## 🔑/⚡/💬/⚠️` 等重复 H2，前端依赖外层 `###` 与正文内的 `##` 切片）→ Step0b 公司快照表 → Step2 公司收入拆分（产品线 + 地理）→ Step3 展望 → Step4 电话会 → Step5 新闻 / 并购 / **内部交易** → Step6 年度财务表 + 季度图。

### 6.2 板块业务构成分布（Step 0，LLM）

- 在整份报告生成流程中**较早执行**，减轻 Step3/4 等并发 LLM 阶段的速率限制。
- 使用 **`ANTHROPIC_MODEL`**（**不**传 `task="finance"`）。
- **输入信息丰富化**：`core_business` 截取长度从 150 字扩展到 **300 字**，给 LLM 更具体的业务信息。
- **Prompt 格式约束**：强制要求"为[客户类型]提供[产品/服务名称]"句式，必须列举①具体产品名称②客户行业③代表性数字（金额/规模/份额）；**禁止使用空泛词汇**（高附加值 / 综合解决方案 / 全方位 / 一体化等）。
- **完整性校验**：对输出做 ticker 子串检查（简短形式取每行首词 ticker），缺公司**最多重试 5 次**；另对疑似 output 截断走续写（`_is_truncated_llm_output`）。
- **后处理**：过滤 LLM 自述前缀（「修正版」「以下是重新…」等）、**自我核查块**（`---` 后「核查 / 已检查 … 全部覆盖 / 涵盖」），最终 `report_md` 上再扫一遍。
- 已知问题：Sonnet 仍有少量截断概率；缓存命中后稳定，重新生成时偶尔不全。

### 6.3 非 LLM 数据优先级（Bloomberg → FMP / SEC）

以下与 `bloomberg_reader` + `sector_report_service` + `insider_service` 当前实现完全一致；未配 DO PG 或 `BLOOMBERG_DB_ENABLED=0` 时自动走回退链。

**产品线 / 业务分部**（`_get_revenue_segments`，用于公司卡片、Step2、简介一致性校验）：

1. **Bloomberg `segment_revenue`** → 来源标签 **`Bloomberg PG_REVENUE`**。只取**最近一个财年**；**丢弃 `revenue < 100`（百万美元）** 的占位 / 调整项后重算占比，`absolute` 单位转回美元（与 FMP 行结构对齐）。
2. **FMP** `get_segment_revenue`（多年份尝试）。
3. **画像** `revenue_by_segment`（SEC 10-K/20-F 文本提取）。

Step 2 板块总览的「数据来源」标注是基于公司池实际命中来源动态拼接的 `source_labels` 集合（如同时出现 `Bloomberg PG_REVENUE` 与 `FMP Revenue Segmentation`，会一起列出）。

**地理收入拆分**（Step2 与公司卡片 H-11 两处渲染）：

1. **Bloomberg `get_geo_revenue`** → 脚注 **PG_REVENUE**；两处渲染均加 **`float(r["revenue"]) >= 1`** 过滤，剔除 Bloomberg 浮点 null（约 `-2.4e-14`）与负数调账项；若仅有 `Worldwide` 一行，按公司类型给提示：
   - **HCA**：在表格前提示「以下为 HCA 内部管理分部口径（American/Atlantic/National Group），非传统地理区域划分，全部位于美国境内」。
   - **DHL / JLL 等**：提示「Bloomberg PG_REVENUE 未返回地理分拆数据，如需查阅请参考公司年报」。
2. **FMP** `get_geographic_revenue`（保留原覆盖率 / 产品线去重等逻辑）。

**财务数据来源标签**（`_get_validated_financials`，返回值现为三元组 `(rows, issues, source_label)`）：

- `source_label` 取值 `"Bloomberg"` 或 `"FMP"`，反映实际命中的数据源。
- **板块概览「数据覆盖表」**：`_fin_sources_check` 除收集各公司 `_get_validated_financials` 的 `source_label` 外，若 **`get_profile(ticker).data_source_label`** 文案中含 **`Bloomberg`**，也会把 Bloomberg 纳入集合，用于与纯 FMP 财务行并列时的展示（与 Step 6 文案一致：`Bloomberg Annual Financials` / `Bloomberg（优先）/ FMP（回退）` / `FMP Annual Financials`）。
- Step 6（年度财务表）底注根据**全池子实际命中来源集合** `fin_sources_used` 动态拼接（规则同上）。
- **执行摘要**内「参考来源」中的财务快照一行单独用 **`_es_fin_sources`** 构建，与概览表同源逻辑。
- Step 0b 公司快照表底注固定为 `"Bloomberg / FMP Annual Financials（最新财年，Bloomberg 优先）"`，反映优先级而非每家命中情况。

**新闻类来源标签**（`services/signal_fetcher.py` → 行业报告 **`_news_src_label`**）：

- 单条新闻若来自 Bloomberg RSS，`query_axis` 会带 `bloomberg_rss`；下游将 Bloomberg 与 Benzinga 一并纳入提供者集合，**Step 5 脚注、执行摘要「重要事件」、参考来源表**等处使用同一动态标签。
- **Bloomberg RSS 为头条订阅**（markets / technology / industries 等 feed，合计约数十条级），**不是**历史全文搜索 API；监控池内仅**恰好出现在头条里**的标的易命中，命中率天然偏低（详见 `claude.md`「数据源限制」）。

**内部交易汇总**（`insider_service.get_insider_summary`，行业报告 Step 5 与公司详情）：

1. **Bloomberg `get_insider_monthly`**：按 `MM/YYYY` 与 `days_back` 截断；返回 `"source": "Bloomberg INSIDER_MONTHLY_TRANSACTION"`，**无逐笔 `trades`、无 `top_insiders`**（月度聚合）。
2. **FMP** `get_insider_trades`。
3. **美股**回退 **`get_insider_trades_sec_form4`**（SEC EDGAR Form 4 XML）；`primaryDocument` 含 `xslF345X06/` 等路径时会**剥到真实 `form4.xml`** 再请求。

**单位约定**（三路径必须一致，写入 `get_insider_summary` docstring）：

- `total_buy_value` / `total_sell_value` / `net_value` / `top_insiders[*].total_value` 统一为**美元（USD）裸数字**。
- Bloomberg 路径：`shares_bought × close_price` ⇒ 股 × USD/股 = USD。
- FMP / SEC Form 4 路径：优先 `totalValue`（Form 4 标准为 USD），缺失时用 `shares × price` 推算；RSU/Phantom Stock 归属或行权释放（`price=0` 且 `totalValue=None`）被识别为"无可统计 notional"忽略，不汇入买卖金额。

`get_insider_summary` 内部仍用 `_bbg_attempted` / `used_sec_form4` 等拼出**完整 `source` 链路**（用于日志与公司详情调试）。**Step 5 板块级引用块脚注**为固定简述，避免拼接过长：`Bloomberg INSIDER_MONTHLY（优先）/ FMP / SEC Form 4（回退）`（与新闻脚注分列展示）。

**金额格式化**（`_fmt_trade_side_value`，sector_report_service.py）：

- 该侧无交易（`trade_count <= 0`）→ `—`
- 有交易但 notional 全部缺失（`buy_val=None` 或 `sell_val=None`）→ `金额未披露`
- 计算结果 `|value| < $1000` → 在数字后附「（金额异常偏小，可能为受限股归属/期权行权，请核查原始 Form 4 申报）」，因为通常对应 RSU/Phantom Stock 归属、期权行权释放等非市场交易残值

Step 5 **公司小节**等仍可使用 `insider["source"]` 等结构化字段；**板块级统一脚注**见上文固定文案。

### 6.4 执行摘要（`_executive_summary`）后处理

- 四个子块文本（核心主题 / 重要事件 / 管理层关键信号 / 主要风险）在写入总览前经 **`_strip_redundant_h2`**：剔除 LLM 自加的 `## 🔑/⚡/💬/⚠️/…` 行，避免破坏前端按 `##` 切 Tab。
- **`_filter_non_sector_content`**：按行内显式 ticker 引用（`[TICKER]`、`（TICKER）`、行首 `- TICKER —`、`(TICKER)` 等）与白名单比对；**四块全部过滤**（含核心主题与主要风险，避免其它板块公司名渗入）。匹配模式为 **`[A-Z0-9/]{1,6}`**，支持 `[3M]`、`[BT/A]` 等。
- **核心主题**：prompt 约束 + **`_dedup_theme_bullets`** 双遍扫描，去掉同一 ticker 的重复空占位行。
- **`_strip_unauthorized_sections`**：用于管理层关键信号、主要风险等，删掉「编辑注」「免责声明」等模型自加章节。

### 6.5 LLM 输出过滤与占位文字（Step 3 / Step 4 / Step 5）

**Step 3 行业判断**（`_step3_per_company_outlook`，每家公司的 `industry_view` 渲染）：

收敛到唯一占位「`**行业判断（管理层视角）：** *原文未明确提及行业判断*`」。三种触发占位的情形：

1. `iv` 为空 / 命中哨兵值（`"原文未明确提及"` / `"NOT_FOUND"`）
2. `iv` 是 10-K Item 1A 风险因素模板句（含「forward-looking statements」「actual results may differ」等 8 个英文短语）
3. `iv` 命中 **LLM 自我回避表述词典**（16 条中文模式：「无法归纳」「未对行业趋势」「未作出明确判断」「未检出明确」「不构成明确判断」「原文未明确」等），命中即把 `iv` 置空走 else 分支

`max_tokens` 提升到 **3000**（板块汇总 19 家公司内容量大，1200 不够），同时实现`_is_truncated_llm_output` 检测（覆盖句末非完整结束标点、未闭合中文/英文 `（`、未闭合 `[TICKER]` 三类），命中后走 `_run_s3_block` 续写循环。

- **「未来展望 / 行业观点」段落小标题**（`_one_company_block`）：对 `future_guidance`、`industry_view` 取**首句**截约 **20 字**生成行首小标题，纯字符串处理、无额外 LLM。
- **管理层引用展示**：画像模型里可为关键引用带 `data_source` / `fmp_transcript_url` 等字段；**Step 3 渲染**以可读性优先，展示 `来源：Earning Call 逐字稿（…）` / `来源：10-K …` 等文字，**不渲染 FMP 外链**（字段可保留备后续用途）。

**Step 4 板块总结**（`_run_s4_block`，三个子区块）：

- `_s4_guard` 已**放宽**：从原"必须有具体数字"改为「**优先引用管理层原话或具体数字，若无精确数字则可引用管理层明确表述的战略方向或定性判断**」——避免无精确数字的公司被排除。
- 三个区块 `max_tokens` 大幅上调（19 家公司每家平均 60+ token 远不够）：
  - **AI 部署与替代进展** `s4_ai`：`max_tokens=2000`，prompt 加硬约束「必须为每一家有数据的公司单独输出至少一条 bullet，不得遗漏」
  - **营收与盈利关键数据** `s4_fin`：`max_tokens=1500`，同覆盖硬约束
  - **人员与组织变动** `s4_people`：`max_tokens=1200`
- 续写循环最多 3 轮（疑似截断或长度 < 60 字符时触发）。
- **参考来源行**：删除「关键数据：xxx」裸数字（关键数字已在各公司详情正文完整呈现），改用：

  ```text
  - **Cognizant (CTSH)**：Earning Call 逐字稿 2026Q1，来源：FMP API｜Quotations：8 条原话 ｜ 管理层观点：10 条
  ```

- **季度标注用 FMP 实际命中**（见 §7.2），与请求季度不同时显式标注：

  ```text
  Earning Call 逐字稿 2025Q4（请求 2026Q1，FMP 实际命中 2025Q4），来源：FMP API｜…
  ```

**Step 5 板块汇总**（新闻 / 并购 / 内部交易）：

- Prompt 强制要求**每条 bullet 必须以 `[公司代码]` 开头**（如 `[ACN] 事件描述（金额）— 来源：Benzinga/Earning Call`），**禁止用公司名作为小标题再在下方写 bullet**——容易导致张冠李戴。
- 加 `{coverage_tickers}` 硬约束：「必须为以下每一家公司单独输出至少一条内容，不得跳过或合并省略」，覆盖率从 5 家→ 12+ 家。
- `_chat5` `max_tokens=2000`、`timeout=300.0`，续写时同步使用 2000 上限。
- **`_strip_step5_embedded_disclaimers`**：去掉正文中复读的免责/固定来源行、公司间误插入的 `---`，并清理 **空 `###` 标题行**与 **无正文的连续 `**小标题**` 行**。
- 输出侧依次 **`_sanitize_bracket_tickers`**、**`_filter_by_ticker_whitelist`**、**`_filter_non_sector_content`**（监控池集合），与执行摘要侧过滤策略一致。

### 6.6 Ticker 白名单（Step 5 等板块正文）

`_filter_by_ticker_whitelist` 把白名单外 **`[TICKER]`** 引用涂改为 `〔非本板块标的〕`，或整行丢弃（见下）：

- **Rogue 识别仅看方括号**：`\[([A-Za-z]{2,6})\]` 与允许集合比对；**裸全大写词不再作为 rogue 来源**（避免 `ARM` / `NVIDIA` / 品牌名每换板块就要扩 `_COMMON_ABBREVS` 的打地鼠问题）。
- 若一行仅有白名单外 bracket、且同行无本板块合法引用 → **丢弃整行**；若标题行被丢弃 → **`_skip_section`** 联动跳过其后内容直到下一标题 / `---`，避免孤立 bullet。
- 涂改路径仍可对行内出现的允许 ticker 与 rogue 共存情况做 **仅替换方括号内 rogue**（`word` 边界匹配用于**判断**行内是否仍含本板块 ticker，非用于扫 rogue）。

Step 5 另有一道 **警告用**扫描：对 `\b[A-Z]{2,6}\b` 与 `_COMMON_ABBREVS` 做差集打日志；**正文过滤**以方括号白名单与 `_filter_non_sector_content` 为主。

### 6.7 财务校验（`_validate_and_sanitize_financials`）

规则化校验，异常时暂停字段自动填入（置 `None`）：

| 规则 | 条件 | 处理 |
|---|---|---|
| **EBITDA < Net Income** | 见下方三段豁免 | 命中且不豁免 → 清空 `ebitda` |
| **净利率上限** | `ni / revenue > 40%` 或 `> 35%`（非软件例外公司） | 清空 `net_income` |
| **Net Income 双源** | FMP vs SEC EDGAR XBRL 偏差 > 2% | 清空 `net_income` |
| **Revenue 电话会双源** | FMP vs 管理层口述数字偏差 2%-50% | 清空 `revenue` |
| **联动清空** | `revenue` 已清空 → `ebitda` / `gross_margin` 同步清空 | |
| **毛利率波动** | 同比变化 > 500bps（规则 3） | 见 docstring |

**EBITDA < Net Income 三段豁免**（避免 SBC 重 SaaS / 一次性税收抵免等系统性误报）：

1. **条件 A（both_negative）**：`EBITDA < 0` 且 `NI < 0`——亏损期 SBC 重 SaaS 公司（如 **MDB**），D&A + 利息 + 税合并调整后 EBITDA 反而更亏，方向一致时不视为错误。
2. **条件 B（small_gap）**：`|NI - EBITDA| < revenue × 10%`——小额一次性项，属正常财务波动。
3. **条件 C（one_time_credit）**：两者均为正且 `NI ≤ EBITDA × 2.0`——大额一次性税收抵免 / 递延税资产释放 / 投资收益（如 **ZM**：NI/EBITDA = 1.48），上界 2.0 倍仍能截住 `NI = 5× EBITDA` 等真实数据错位。

三条豁免任一命中即跳过告警并保留 EBITDA；`logger.debug` 级别记录每次豁免。

### 6.8 缓存与并发

| 层级 | 说明 |
|------|------|
| `sector_report_cache` | 整份 Markdown（sector + 年 + 季） |
| `step_cache`（**`cache_version=6`**） | 各 step 的 JSON 行列表：`overview`、`step3`、`step4`、`exec_summary` 等；`force_refresh` 时跳过本层 |
| `step_cache`（`__profile__` / `business_profile`，**`cache_version=1`**） | 业务画像，按 ticker；写入前对 `industry_view` 做截断检测+最多 2 轮续写 |
| `step_cache`（`__earnings__` / `step4_analysis`，**`cache_version=3`**） | 电话会分析，按 ticker + 年 + 季；缓存键用请求季度，存储值的 `analysis.quarter` 反映实际命中 |
| 同 sector **in-flight 锁** | 避免重复大模型调用 |

**缓存命中行为**：在 `force_refresh=False` 且整份缓存命中时，会直接返回缓存里的 **Markdown 文本**；只会重新跑 `_load_per_company_signals_and_insiders` 与 `_step6_quarterly_charts_section` 以重建前端的季度图数据，**报告正文不会刷新**。如果你刚改了 Bloomberg/Step 2/Step 5 等代码却没看到效果，先确认是否需要 `force_refresh=True` 或手动清缓存（见 §8）。

财务进 LLM 前有校验，生成后有 `post_generation_checker` 等；细节见代码注释。Step 3「行业判断」：有 `industry_view_source` 时输出 `*来源：…*`，无来源时回退为统一占位「原文未明确提及行业判断」。

### 6.9 Step 6 图表（`chart_service.build_quarterly_sector_charts`）

`figs = build_quarterly_sector_charts(quarterly_data, sector_name)` 返回 6 张图（顺序对应 `04_SectorReport.py` 渲染槽位 0–5）。

**覆盖阈值过滤**（所有 6 张图）：

在汇总各季度数据前，先计算 `min_coverage = max(1, total_companies // 2)`（**至少 50% 公司有数据**）；覆盖不足的季度直接从 `quarters` 列表中剔除——避免最新季度部分公司未披露财报时出现「断崖效应」（汇总值偏低）。

**图1（CAPEX 2-Period ROC 折线）已做极值处理**：

- **纵轴 p75 截断**：`y_max = max(p75 × 2.0, 2.0)`、`y_min = -0.5`（正值场景）或 `min × 1.3`（含负值场景）——比 p95 更激进，单期 35× 跳幅不会再把其他季度全部压在 0 轴附近。
- **极值注释（遍历所有截断点）**：超出截断范围的**每个**点都用 plotly 箭头标注，文字为「峰值 +30.8」/「谷值 -X.X」；正向越界 ay=-30，负向越界 ay=+30。
- **横轴标签抽稀**：季度数 > 10 时每隔一个标签显示一次（保留 `tickangle=-45` 与其他子图一致）。
- **高度 380**（其他 5 张图保持 _base.height=320）。
- p75 用纯 Python 线性插值实现，不依赖 numpy。
- **图3（Overlay: GM vs CAPEX ROC）的右轴**复用图1的 `roc_range`，保持双图视觉一致。

---

## 7. 业务画像与电话会（数据源要点）

### 7.1 画像 `profile_service.get_profile`

- 主路径：SEC **10-K / 20-F** 节选 + LLM，走完整 `_build_prompt`；主生成调用显式 **`max_tokens=4000`**（大公司 JSON 字段多，`2000` 曾导致 CTSH 等解析失败；**续写**调用仍为 **`max_tokens=1500`**）。
- **FMP 回退**：`SecEdgarError`、合并后为空、或 **SEC 内容质量差**（过短/过长/缺关键词/疑似高管简历页）时，拉 **FMP `/stable/profile`** 的 `description`；映射表 `_FMP_PROFILE_TICKER_MAP` 与 **`symbol` 自身** 均可作为 FMP symbol。
- **`_fmp_desc_used`**：纯 FMP 描述驱动时改用 `_build_fmp_prompt`，只要求 JSON 中以 `core_business` 为主，其余字段多为空数组 / null。
- **`industry_view` 截断检测 + 续写**：写入 `set_step_cache` 之前，对 `payload["industry_view"]` 调本地 `_is_truncated_llm_output`（与 sector_report_service 同语义，含中文/英文 `（` 未闭合检测），疑似截断时调 `chat()` 续写，最多 2 轮；写入哨兵值 `"原文未明确提及"` 时跳过续写。
- **`_enrich_guidance_from_earnings_call` 内容质量校验**：当 10-K 未抽到 `future_guidance` / `industry_view` 时从电话会逐字稿补抽，写入前用 `_is_meaningful(raw, keywords)` 校验：
  - `future_guidance` 必须 `len ≥ 20 + 不疑似截断 + 含至少一个指引动词`（预计 / 计划 / 目标 / 可能 / 指引 / 展望 / 承诺 / 重申 / outlook / expect / plan 等）。
  - `industry_view` 必须 `len ≥ 20 + 不疑似截断 + 含至少一个行业判断关键词`（行业 / 竞争 / 需求 / 供给 / 市场 / 宏观 / 政策 / industry / market 等）。
  - 不满足时写入说明性占位（`【电话会YYYYQX】逐字稿已获取但未检出明确指引表述，请查阅原文`），避免输出半句「管理层提出」。

### 7.2 电话会 `earnings_service.analyze_earnings_call`

- 逐字稿：**FMP**（`stable/earning-call-transcript`）→ **EDGAR 8-K** → **sec-api.io**（需 `SEC_API_KEY`）。
- **非美股 FMP symbol 映射**：`_FMP_TRANSCRIPT_TICKER_MAP`（含 `DHL GY` → `DHL.DE`、`BT/A LN` → `BT-A.L`、`FRE GY` → `FRE.DE`、`KBX GY` → `KBX.DE`、`RTO` → `RTO` 等）。
- **映射表内标的**：FMP 无稿时**不再尝试 SEC 8-K**（避免无效请求）；未映射标的仍走 SEC 链路。
- 无逐字稿：抛 `EarningsAnalysisError`，API 返回 **503**。
- **`analysis.quarter` 反映 FMP 实际命中**：FMP 原始响应含 `year` + `period`（如 `Q4`），可能与请求季度不同（如请求 2026Q2 时 FMP 仅有 2025Q4）。`fmp_client.get_earnings_transcript` 现在返回 `quarter` / `requested_quarter` / `fiscal_year` / `fiscal_quarter` / `date` 多字段；`earnings_service` 在 FMP 命中分支用实际财季覆盖 `qlabel`（不一致时 INFO 日志：`电话会请求 X 实际命中 Y`）。SEC 回退路径无可靠财季元数据，保留请求季度。

### 7.3 `inject_transcripts.py`

- 将本地 **Bloomberg PDF** 抽文本后分段入库并调 LLM，写入 **`step_cache`**（`sector=__earnings__`、`step=step4_analysis`）。
- 需修改 `PDF_MAP` 中 `path` / `year` / `quarter`；`source` 可按需标为 `sec_8k` 等与前端展示一致。

---

## 8. 数据库与缓存

| 类型 | 位置 | 说明 |
|------|------|------|
| 主库 | `data/research.db` | `companies`、`step_cache`、`sector_report_cache`、`document_paragraphs` 等 |
| 整份行业报告 | `sector_report_cache` | 按 sector + 年 + 季 |
| Step 片段 | `step_cache` | `overview` / `step3` / `step4` / `exec_summary` 等（`cache_version=6`，版本号 bump 后旧缓存自动失效） |
| 业务画像 | `step_cache`（`sector='__profile__'`） | 按 ticker；`cache_version=1` |
| 电话会分析 | `step_cache`（`sector='__earnings__'`） | 按 ticker + year + quarter；`cache_version=3` |
| 段落溯源 | `document_paragraphs` | 画像 / 电话会分段 ID |

### 8.1 清缓存

最常见的「改了代码但前端没变」就是缓存遮盖：**`sector_report_cache` 与 `step_cache` 必须一起清**，否则下次跑会从 `step_cache` 重建旧报告。

```bash
# 清单 step（如 exec_summary）：
PYTHONPATH=src python3 -c "
from research_automation.core.database import clear_step_cache
print('deleted', clear_step_cache('AI_Job_Replacement', step='exec_summary'))
"

# 清整份报告缓存（按年季）：
PYTHONPATH=src python3 -c "
from research_automation.services.report_cache import delete_report_cache
delete_report_cache('AI_Job_Replacement', 2026, 1)
"

# 一键清同一 sector 的所有 step + 整份缓存（最常用）：
PYTHONPATH=src python3 -c "
from research_automation.core.database import get_connection
conn = get_connection()
conn.row_factory = None
conn.execute(\"DELETE FROM sector_report_cache WHERE sector='AI_Job_Replacement'\")
conn.execute(\"DELETE FROM step_cache WHERE sector='AI_Job_Replacement'\")
conn.commit()
conn.close()
print('done')
"

# 清单家公司画像缓存（如 JLL 修复后让画像重生成）：
PYTHONPATH=src python3 -c "
from research_automation.core.database import get_connection
conn = get_connection()
cur = conn.execute(
    \"DELETE FROM step_cache WHERE sector='__profile__' AND step='business_profile' AND ticker=?\",
    ('JLL',),
)
print('deleted profile rows:', cur.rowcount)
conn.commit(); conn.close()
"

# 清电话会分析 + Step 3/4 渲染缓存（FMP 实际命中季度等变更后）：
PYTHONPATH=src ./venv/bin/python3 -c "
from research_automation.core.database import get_connection
conn = get_connection()
cur1 = conn.execute(\"DELETE FROM step_cache WHERE sector='__earnings__'\")
cur2 = conn.execute(\"DELETE FROM step_cache WHERE step IN ('step3_per_company_outlook','step4_earning_call_section') OR step LIKE 'sector_report%'\")
print('cleared earnings:', cur1.rowcount, 'sector_report:', cur2.rowcount)
conn.commit(); conn.close()
"

# 查看各 step 缓存行数（排查"为什么没清干净"）：
PYTHONPATH=src python3 -c "
from research_automation.core.database import get_connection
conn = get_connection()
r1 = conn.execute('SELECT COUNT(*) FROM sector_report_cache').fetchone()[0]
print(f'sector_report_cache: {r1}')
rows = conn.execute('SELECT step, COUNT(*) FROM step_cache GROUP BY step').fetchall()
for row in rows:
    print(f'step_cache [{row[0]}]: {row[1]}')
conn.close()
"
```

清完后：

- 重启 / 重新发起报告生成（界面强制刷新 或 `force_refresh=True`）。
- **Streamlit** 需在浏览器里 **Cmd+R / Ctrl+R**（`st.session_state` 会缓存旧 Markdown）。
- 改 `.env` 后请重启 **uvicorn** 与 **Streamlit**。

### 8.2 导出 / 导入 `__earnings__` 缓存（本地 → 服务器）

`step_cache` 表列名为 **`generated_at`**（不是 `created_at`），主键为 **`(sector, year, quarter, step, ticker)`**。在项目根目录执行（`data/research.db` 路径按你部署调整）。

**本地：导出**

```bash
PYTHONPATH=src python3 -c "
import sqlite3, json
conn = sqlite3.connect('data/research.db')
conn.row_factory = sqlite3.Row
rows = conn.execute(\"SELECT * FROM step_cache WHERE sector='__earnings__'\").fetchall()
data = [dict(r) for r in rows]
with open('earnings_cache_export.json', 'w', encoding='utf-8') as f:
    json.dump(data, f, ensure_ascii=False)
print(f'导出 {len(data)} 条')
conn.close()
"
```

**传输**（示例）：

```bash
scp earnings_cache_export.json user@your-droplet:/opt/research_app/
```

**服务器：导入**（在含 `data/research.db` 的目录执行，`PYTHONPATH` 指向 `src` 与否均可，此处直接用 `sqlite3`）：

```bash
cd /opt/research_app   # 示例
venv/bin/python3 -c "
import sqlite3, json
with open('earnings_cache_export.json', encoding='utf-8') as f:
    data = json.load(f)
conn = sqlite3.connect('data/research.db')
for row in data:
    conn.execute('''
        INSERT OR REPLACE INTO step_cache
        (sector, year, quarter, step, ticker, content, generated_at, cache_version)
        VALUES (:sector, :year, :quarter, :step, :ticker, :content, :generated_at, :cache_version)
    ''', row)
conn.commit()
print(f'导入 {len(data)} 条')
conn.close()
"
```

导入前请确认服务器 SQLite schema 与本地一致（同一代码版本迁移的 `research.db`）。若 JSON 来自旧库缺列，需先 `ALTER TABLE` 或改导出字段列表。

---

## 9. 环境变量

```bash
cp .env.example .env
```

下表与 `llm_client` / `sector_report_service` / `.env.example` 一致；Anthropic 相关键可在 `.env` 中手动补充。

| 变量 | 用途 |
|------|------|
| `ANTHROPIC_API_KEY` | Claude 优先路径 |
| `ANTHROPIC_MODEL` | 默认 Claude 模型；**板块业务构成分布**（Step 0）用此模型，建议 Sonnet（参考：`claude-sonnet-4-6`） |
| `ANTHROPIC_MODEL_FINANCE` | `chat(..., task="finance")` 用，行业报告其余金融向 LLM 步骤；未设则同 `ANTHROPIC_MODEL`，建议 Opus 系（如 `claude-opus-4-7`；`.env.example` 占位可能与控制台实际模型名略有差异，以账号可用名为准） |
| `OPENAI_API_KEY` | LLM 回退或部分调用 |
| `SEC_EDGAR_USER_AGENT` | SEC 规范（含联系邮箱）；10-K/8-K/Form 4 等必需 |
| `SEC_API_KEY` | 可选，sec-api.io 全文检索 |
| `FMP_API_KEY` | FMP `stable/` 接口；财务、内部交易、电话会逐字稿（逐字稿多为付费层，402 会自动回退其它来源） |
| `DO_DB_HOST` / `DO_DB_PORT` / `DO_DB_NAME` / `DO_DB_USER` / `DO_DB_PASSWORD` / `DO_DB_SSLMODE` | DigitalOcean PostgreSQL 只读库连接（Bloomberg schema） |
| `BLOOMBERG_DB_ENABLED` | `0` / `false` / `off` 时不连 DO PG，整体回退 FMP/SEC |
| `DO_DB_CONNECT_TIMEOUT` | PostgreSQL 连接超时（秒），默认 `8` |
| `BENZINGA_API_KEY` / `FINNHUB_API_KEY` | 公司新闻 |
| `TAVILY_API_KEY` | 可选，信号 POC |
| `NEWS_TIMEZONE` | 隔夜 / 昨日时间窗（默认 `America/New_York`） |
| `REPORT_RELEVANCE_THRESHOLD` | 行业报告新闻 relevance 下限（0–3） |
| `SECTOR_REPORT_STRICT_LLM` | 设为 `1` / `true` 等时，板块级 LLM 失败**不吞异常**，便于调试 |

---

## 10. 安装与启动

**环境**：建议使用 **Python 3.11+**（本仓库开发与 `py_compile` 多为 **3.12**）。

```bash
python3 -m venv venv
source venv/bin/activate          # Windows: venv\Scripts\activate
pip install -r requirements.txt
cp .env.example .env
# 编辑 .env：至少 SEC_EDGAR_USER_AGENT、ANTHROPIC 或 OPENAI、按需 FMP/新闻/Bloomberg
```

### 10.1 依赖版本（`requirements*.txt` 下限）

根目录 **`requirements.txt`** 包含 **`src/research_automation`** 后端依赖与 **`frontend/requirements.txt`**，并额外要求 `json-repair>=0.30.0`、`psycopg2-binary>=2.9.9`（Bloomberg 只读库可选）。

| 区域 | 主要包（下限示例） |
|------|---------------------|
| 后端 | `fastapi>=0.109`、`uvicorn[standard]>=0.27`、`pydantic>=2`、`pandas>=2`、`openai>=1`、`earningscall>=1`、`feedparser>=6`、`beautifulsoup4>=4.12`、`lxml>=5` |
| 前端 | `streamlit>=1.31`、`requests>=2.31`、`plotly>=5.18` |
| 开发 | `requirements-dev.txt`：在上式基础上 `pytest>=8` |

具体以各文件为准；升级 major 版本前建议跑 `pytest` 与一份完整行业报告烟测。

### 后端（必须 `PYTHONPATH=src`）

```bash
cd /path/to/project
source venv/bin/activate
PYTHONPATH=src python3 -m uvicorn research_automation.main:app --reload --reload-dir src --port 8000
```

- **`--reload-dir src`**：避免监视 `venv/` 导致 uvicorn 反复重载。
- macOS 上若 `python` 不存在，请用 **`python3`**。
- 建议使用 **`python3 -m uvicorn`**，确保使用当前 venv。

### 前端（必须用项目 venv 里的 Python）

macOS 上 `which python3` 仍可能指向 `/Library/Frameworks/...`，导致缺 `plotly`。请使用：

```bash
cd /path/to/project
./venv/bin/python3 -m streamlit run app.py --server.port 8501
```

或：

```bash
./run_streamlit.sh         # 默认 8501
./run_streamlit.sh 8502    # 须与 main.py CORS 一致
```

- **路径含空格**：`cd` 时对整个项目路径加引号。
- **首次启动**：冷导入可能 1～3 分钟无输出。
- **性能**：`.nosync` 仅减少 iCloud 同步；大仓库建议放在 `~/Developer/...` 等非桌面路径。

- API：<http://127.0.0.1:8000/docs>
- Streamlit：<http://127.0.0.1:8501>（或自定义端口）

---

## 11. 运维命令

```bash
source venv/bin/activate
python3 inject_transcripts.py
PYTHONPATH=src python3 test_bbg.py                # 分部数据来源烟测（见文件内 ticker 列表）
python3 -m py_compile src/research_automation/services/sector_report_service.py
```

### Bloomberg 数据快速烟测

```bash
# 三个核心读取函数
PYTHONPATH=src python3 -c "
from research_automation.extractors.bloomberg_reader import (
    get_segment_revenue, get_geo_revenue, get_insider_monthly,
)
print('seg DHL :', len(get_segment_revenue('DHL')))
print('geo IBM :', len(get_geo_revenue('IBM')))
print('ins BT/A:', len(get_insider_monthly('BT/A')))
"

# Insider 汇总（看 source 链路）
PYTHONPATH=src python3 -c "
from research_automation.services.insider_service import get_insider_summary
s = get_insider_summary('BT/A LN', days_back=180)
print(s['source'], s['buy_count'], s['sell_count'])
"

# 行业报告分部数据入口
PYTHONPATH=src python3 -c "
from research_automation.services.sector_report_service import _get_revenue_segments
rows, year, source = _get_revenue_segments('DHL')
print(source, year, len(rows))
"

# 财务数据来源标签（_get_validated_financials 第3返回值）
PYTHONPATH=src python3 -c "
from research_automation.services.sector_report_service import _get_validated_financials
rows, issues, src = _get_validated_financials('ACN', years=3)
print('source:', src, 'rows:', len(rows), 'issues:', issues)
"

# 财务校验跑全池子（看 EBITDA<NI 等告警）
PYTHONPATH=src python3 -c "
from research_automation.services.sector_report_service import _get_validated_financials
for t in ['CTSH','DG','EL','IBM','MDB','PPG','TGT','UPS','ZM']:
    rows, issues, src = _get_validated_financials(t, years=1)
    if issues:
        print(t, src, issues)
"

# FMP 实际命中季度
PYTHONPATH=src python3 -c "
from research_automation.extractors.fmp_client import get_earnings_transcript
r = get_earnings_transcript('CTSH', 2026, 1)
print({k: r.get(k) for k in ('quarter','requested_quarter','fiscal_year','fiscal_quarter','date')})
"

# 信号过滤验证（看 TGT 等通用词 ticker 必含关键词检查）
PYTHONPATH=src python3 -c "
from research_automation.services.signal_fetcher import _passes_required_keywords
print(_passes_required_keywords({'title':'Target Corporation reports Q4','content':'','url':''}, 'TGT'))   # True
print(_passes_required_keywords({'title':'Activist Builds Stake in Takeover Target','content':'','url':''}, 'TGT'))  # False
"
```

---

## 12. 测试

```bash
source venv/bin/activate
PYTHONPATH=src pytest tests -q
PYTHONPATH=src pytest tests/services/test_post_generation_checker.py -q
```

---

## 13. 局限与已知问题（交接必读）

| 类别 | 说明 |
|------|------|
| **数据覆盖** | FMP 电话会逐字稿对**非美股 / 部分季度**可能无数据；映射错误会得到空结果。 |
| **Bloomberg / DO PG** | 须在 DO **Trusted sources** 放行 IP / VPN；某些 WiFi 网络封 25060 端口，建议热点或在控制台加 IP。 |
| **Bloomberg API 上限**（已确认无解） | Desktop API `refdata` **无电话会全文**；地理收入**国家级**细分 Segment ID 报 BAD_SEC，只能拿顶层（Americas / EMEA / APAC）；非美股 Insider **个人级别**字段无效，仅月度汇总可用。 |
| **BT/A / KBX 毛利率缺失** | Bloomberg `financials_annual` 无 `gross_profit` 字段（三年全 `None`），Step 6 显示 `—` 为正常行为；待 Windows 端 `bbg_etl.py` 确认是否能拉 `GROSS_PROFIT` 字段。 |
| **SEC Form 4** | 仅适用于 SEC 覆盖的美股；解析依赖合规 **User-Agent** 与申报 XML 结构。 |
| **SEC** | 频率过高可能受限；非美股 20-F 解析质量因公司而异。 |
| **画像** | FMP `description` 非监管披露全文，仅作回退。 |
| **Insider 小额** | RSU/Phantom Stock Units 归属、期权行权释放（`price=0, totalValue=None`）会被识别为无可统计 notional 忽略，**金额异常偏小（如 PPG $862 = 2 笔真实小额市场卖出）**时正文带「请核查原始 Form 4 申报」附注，不是单位 bug。 |
| **EBITDA < NI 豁免** | 三段豁免（both_negative / small_gap / one_time_credit）覆盖 SBC 重 SaaS（MDB）与大额一次性税收抵免（ZM）；NI > 2× EBITDA 仍会被截住。 |
| **板块汇总图末段偏低** | 最新季度部分公司未披露财报会拉低汇总值；已通过 **50% 覆盖阈值过滤**消除大部分断崖，但末 1-2 季度仍可能偏低。 |
| **白名单与全大写词** | **`_filter_by_ticker_whitelist` 仅对 `[TICKER]` 方括号做 rogue 判定**，正文里裸全大写品牌名/术语不再被该行逻辑误删；Step 5 仍可能对全大写词打 **warning 日志**（与 `_COMMON_ABBREVS` 差集）。跨板块公司渗入主要靠 **`_filter_non_sector_content`** 与 prompt 约束缓解。 |
| **Anthropic 额度耗尽** | API 返回 4xx（如余额用尽）时，若应用层将失败 **静默回落为空字符串**，可能出现「执行摘要整段空白」而无 Python  traceback；需看网关日志或临时设 `SECTOR_REPORT_STRICT_LLM=1`。 |
| **Bloomberg RSS** | 见 §6.3：头条订阅、非历史搜索，**命中率天然偏低**；与 Benzinga 互补，不保证监控池全覆盖。 |
| **成本与时延** | 行业报告 LLM 调用多；`max_tokens`、重试与续写会推高费用与耗时（Step 4 三块各 1200-2000 token + 续写最多 3 轮）。 |
| **前端体验** | 长任务多为 `st.spinner`；端口占用自行处理；Streamlit `st.session_state` 会缓存旧 Markdown，需浏览器手动刷新。 |
| **KaTeX 渲染** | `st.markdown` 默认 KaTeX 解读 `$...$` 为 LaTeX；所有承载 LLM 输出的 markdown 必须走 `_safe_md`（`04_SectorReport.py`）转义 `$`。 |
| **缓存遮盖** | 修代码后若仅清 `step_cache` 不清 `sector_report_cache`，下一次仍可能拿到旧报告；最稳妥是 `force_refresh=True` 或一起删（见 §8.1）。 |
| **Markdown 切片** | 报告中同一 ticker 可能出现在「覆盖核查」与公司卡片中；用 `md.find("### TICKER")` 定位卡片，勿用首次 `md.find("TICKER")`。 |
| **Step 0 截断** | Sonnet 仍有概率提前结束；缓存命中后稳定。根本解法考虑改为代码生成分组（需要给每家公司加 `group` 字段）。 |
| **`insider_monthly` 浮点 null** | Bloomberg 会返回 `≈ -2.4e-14` 作为 null 占位，ETL 与 SQL 层均按 `ABS(...) < 1e-10` 过滤；地理收入渲染层另用 `>= 1` 过滤负数调账。 |
| **新闻通用词 ticker** | `TGT/RTO/EL/DG` 等与英文通用词冲突；`signal_fetcher._TICKER_REQUIRED_KEYWORDS` 字典对这些 ticker 要求标题/正文含至少一个实体级关键词，命中记 `dropped_noise` 与 warning 日志。 |
| **新闻抓取覆盖太少** | 很多公司 Benzinga 仍可能无稿；已叠加 **Bloomberg RSS** 仍属头条级覆盖（§6.3）。待继续排查 `signal_fetcher` 与 prompt（仅有标题时禁止臆测细节）。 |
| **注入脚本** | `inject_transcripts.py` 中 `PDF_MAP` 路径多为本机绝对路径。 |
| **合规** | 输出仅供研究；转载数据须遵守各源条款。 |

---

## 14. 常见问题

- **ImportError**：加 `PYTHONPATH=src`，在项目根执行。
- **`plotly` 找不到**：使用 `./venv/bin/python3` 或 `./run_streamlit.sh`。
- **`psycopg2`**：在 venv 中 `pip install -r requirements.txt`；Bloomberg 路径为惰性导入。
- **uvicorn 疯狂 Reloading**：使用 `--reload-dir src`。
- **CORS**：Streamlit 源端口须在 `main.py` 的 `allow_origins` 中。
- **Bloomberg 超时 / 连不上**：换 VPN / DO Trusted sources / 加 IP；或临时 `BLOOMBERG_DB_ENABLED=0`。
- **503 画像 / 电话会**：查 `detail`、`SEC_EDGAR_USER_AGENT`、`FMP_API_KEY`、逐字稿是否存在。
- **金额「$2.2 billion」被逐字符渲染**：渲染层未走 `_safe_md`，把 `$` 转义为 `\$` 后传 `st.markdown`。
- **板块业务构成分布截断**：查日志中 `Claude stop_reason`（如 `max_tokens`）；可调模型或清 `overview` step 缓存后重跑。
- **Bloomberg 分部 / 地理 / Insider 已优先但仍显示 FMP**：先确认是否命中**整份报告缓存**（`force_refresh=True` 或一起删 `sector_report_cache` + `step_cache`）；再确认 `BLOOMBERG_DB_ENABLED` 与 `DO_DB_*` 配置可用。
- **公司卡片 / Step 2 看不到 Bloomberg 地理数据**：在缓存场景下，命中整份缓存只刷新季度图数据，不重写 Markdown；走 `force_refresh=True` 即可。
- **Insider source 字段不显示 Bloomberg**：检查 `get_insider_summary` 是否真的进入了 Bloomberg 分支（`_bbg_attempted=True`）；非美股若 `insider_monthly` 库无行，会得到 `FMP` / `SEC Form 4` 文案。
- **Step 3 行业判断显示「原文未明确提及行业判断」**：可能是 10-K 无 `industry_view_source`、命中风险因素模板句、或 LLM 自我回避表述（16 条词典）；该占位文字是统一收敛后的结果，**不是 bug**。
- **Step 4 季度标注与请求不同**：FMP 实际命中季度可能早于请求（如请求 2026Q2，FMP 仅有 2025Q4），参考行会标注「请求 X，FMP 实际命中 Y」；如需对齐请求季度，直接看请求参数即可。
- **JLL 等公司「未来展望：逐字稿已获取但未检出明确指引表述」**：LLM 对 `future_guidance` 字段输出半句被内容质量校验拦下并改写占位（见 §7.1）；不是数据缺失，是 LLM 在该 transcript 上没识别出强指引动词，建议直接查阅原文。
- **PPG / 类似公司 Insider 金额 $800–$900 附「金额异常偏小」**：通常是 1–2 笔真实小额市场卖出 + 多笔 RSU/Phantom Stock 归属（被忽略）；附注是提醒读者去看 Form 4 原文区分性质，**不是单位 bug**。
- **MDB / ZM 等公司 Step 6 EBITDA 字段为 None**：旧版本现象，已修复（三段豁免）；清掉 step_cache 或 force_refresh 后会重生。
- **Step 4 / Step 5 板块汇总覆盖不全**：Prompt 已加硬约束（必须为每家有数据公司输出至少一条），仍漏的话多半是 LLM 当次随机性；清缓存重跑即可。
- **Step 5 出现「[XXX] …」但 XXX 不在白名单**：`_filter_by_ticker_whitelist` 将方括号内 rogue 涂改为 `〔非本板块标的〕` 或丢行；跨板块公司整行渗入由 **`_filter_non_sector_content`** 处理。裸全大写词不再是该行过滤主路径。
- **板块汇总图最后一个季度突降**：最新季度尚未全部披露财报；50% 覆盖阈值已过滤大部分断崖，剩余偏低属正常。
- **新闻通用词误抓**：`TGT/RTO/EL/DG` 之类必含实体级关键词；如新增类似 ticker 在 `signal_fetcher._TICKER_REQUIRED_KEYWORDS` 里补条目。
- **文档与代码不一致**：以本 README、`src/research_automation/`、`.env.example` 与 `claude.md` 为准。

---

## 15. 合规与外链

- SEC 数据访问规范：<https://www.sec.gov/os/accessing-edgar-data>
- 本项目输出**不构成投资建议**。
