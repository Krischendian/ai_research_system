# AI 投研自动化 API

基于 **Python + FastAPI** 的投研后端：对外仅暴露 **4 个产品 HTTP 接口**（Swagger 可见），内部整合 Bloomberg（可选 PostgreSQL 只读库）、FMP、SEC EDGAR、Benzinga/Finnhub 新闻、RSS 与 **Anthropic（Claude）/ OpenAI** LLM，生成**自动化晨报**与**行业六步报告**。

本仓库**不含内置 Web UI**；请用独立前端（React/Vue 等）或其它客户端调用 `/api/v1/*`。交互式契约见 **`/docs`**。

本文档与代码冲突时，以 `src/research_automation/`、`.env.example` 为准。更细的 Session 交接记录见 `claude.md`。

**仅用于研究与工程验证，不构成任何投资建议。**

---

## 1. 产品能力（2 个功能 × 4 个接口）

| 产品 | HTTP | 说明 |
|------|------|------|
| **自动化晨报** | `GET /api/v1/morning-brief` | 隔夜速递 + 昨日总结（JSON bundle）；可选 `sector` 过滤监控池 |
| **行业报告 · 板块列表** | `GET /api/v1/sector-reports/sectors` | `companies` 表中活跃 `sector` 下拉列表 |
| **行业报告 · 读缓存** | `GET /api/v1/sector-reports/{sector}` | 读 SQLite 整份缓存；`probe_only=true` 仅探测是否有缓存 |
| **行业报告 · 生成** | `POST /api/v1/sector-reports` | 同步生成六步 Markdown；有缓存且未 `force_refresh` 时秒级返回 |

运维（不进 Swagger）：`GET /`、`GET /health`、`GET /docs`、`GET /redoc`、`GET /openapi.json`。

实现核心：

- 晨报：`overnight_service` + `daily_summary_service`（`morning_brief_bundle` 聚合）
- 行业报告：`sector_report_service.generate_six_step_sector_report`（内部仍调用 `profile_service`、`earnings_service`、`financial_service`、`signal_fetcher` 等，**无单独 REST**）

---

## 2. 架构

```text
┌──────────────────────────────────────────────┐
│  你的前端（任意框架）                          │
│  RESEARCH_API_BASE → http://127.0.0.1:8000    │
└────────────────────┬─────────────────────────┘
                     │ HTTP /api/v1/*
                     ▼
┌──────────────────────────────────────────────┐
│  FastAPI  research_automation.main:app        │
│  CORS：localhost/127.0.0.1 :3000 :5173 :8080  │
└────────────────────┬─────────────────────────┘
                     │
     ┌───────────────┼───────────────┐
     ▼               ▼               ▼
 extractors/     services/        core/
 (FMP, SEC,      sector_report,   database,
  Bloomberg,     overnight,       company_manager,
  LLM, news)     profile, …)     sector_config, …
                     │
                     ▼
           SQLite  data/research.db
           data/raw/   10-K、8-K、新闻等本地缓存
           data/reports/  可选导出目录（多为空）
```

**LLM**（`extractors/llm_client.py`）：优先 **Anthropic**（`ANTHROPIC_API_KEY`），否则 **OpenAI**。行业报告内 `task="finance"` 的步骤使用 `ANTHROPIC_MODEL_FINANCE`（未设则同 `ANTHROPIC_MODEL`）。

**Bloomberg 只读库**（`extractors/bloomberg_reader.py`）：`DO_DB_*` 配置完整且未设 `BLOOMBERG_DB_ENABLED=0` 时，财务分部/地理/内部人交易等优先读 PG；否则回退 FMP/SEC。ETL 在 Windows 端单独运行，本仓库只读。

---

## 3. 仓库目录

```text
.
├── .env.example
├── requirements.txt           # 后端依赖
├── requirements-dev.txt       # pytest
├── README.md
├── claude.md
├── pyrightconfig.json
│
├── data/
│   ├── research.db            # 主 SQLite（gitignore）
│   ├── reports/               # 报告导出目录（gitignore 内容）
│   ├── raw/                   # SEC/FMP 等缓存（gitignore，体积大）
│   └── cache/                 # 新闻等 JSON 缓存（gitignore）
│
├── src/research_automation/
│   ├── main.py
│   ├── api/
│   │   ├── openapi_meta.py    # Swagger 仅 4 path
│   │   └── v1/
│   │       ├── morning_brief.py
│   │       └── sector_reports.py
│   ├── core/
│   ├── extractors/
│   ├── models/
│   └── services/              # sector_report_service.py 等
│
└── tests/
```

---

## 4. HTTP API 参考

基址：`http://127.0.0.1:8000`（部署时替换为你的域名）。

### 4.1 `GET /api/v1/morning-brief`

| 查询参数 | 类型 | 默认 | 说明 |
|----------|------|------|------|
| `sector` | string | — | 监控板块，如 `Technology`；过滤公司新闻池 |
| `include_classic_brief` | bool | `true` | 是否额外拉经典 RSS 宏观/公司晨报块；**建议前端传 `false`**（更快） |

响应：`MorningBriefBundleResponse`（`overnight`、`yesterday_summary`、可选 `morning_brief`）。

- 典型耗时：**约 1～3 分钟**（两次 RSS/新闻抓取 + 两次 LLM）；`include_classic_brief=true` 更慢。
- 建议客户端 **timeout ≥ 300s**，并显示 loading。

### 4.2 `GET /api/v1/sector-reports/sectors`

响应：`string[]`，活跃板块名列表。

### 4.3 `GET /api/v1/sector-reports/{sector}`

| 查询参数 | 类型 | 默认 | 说明 |
|----------|------|------|------|
| `year` / `quarter` | int | 上一完整财季 | 缓存键 |
| `probe_only` | bool | `false` | `true` 时只探测是否有缓存（不返回 `report_md` 正文） |

- 有缓存：200，`from_cache: true`
- 无缓存：404
- 读正文时 `quarterly_data` 可能为空对象；完整生成请用 POST

### 4.4 `POST /api/v1/sector-reports`

Body（JSON）：

```json
{
  "sector": "AI_Job_Replacement",
  "force_refresh": false,
  "relevance_threshold": 1
}
```

响应：`SectorReportResponse`（`report_md`、`quarterly_data`、`from_cache` 等）。

- **无缓存或 `force_refresh=true`**：同步跑完整流水线，约 **15～20 分钟**；建议 timeout **≥ 3600s**。
- **有缓存且 `force_refresh=false`**：直接读 SQLite，通常 **数秒**。

交互文档：启动后打开 <http://127.0.0.1:8000/docs>。

---

## 5. 前端集成要点

1. 环境变量（前端侧）：`RESEARCH_API_BASE=http://127.0.0.1:8000`
2. **CORS**：`main.py` 已允许 `3000` / `5173` / `8080`；其它端口需在 `main.py` 的 `allow_origins` 中追加。
3. **晨报**：务必 `include_classic_brief=false`，除非需要第三块经典晨报。
4. **行业报告**：进页用 `GET ...?probe_only=true` 提示是否有缓存；按钮用 `POST` 生成/读取。
5. **渲染**：`report_md` 为 Markdown；按 `##` / `###` 切片展示；`quarterly_data` 供自行绘图（本 API 不返回 Plotly 图）。

---

## 6. 行业报告流水线（摘要）

实现：`src/research_automation/services/sector_report_service.py`（`generate_six_step_sector_report`）。

输出顺序（Markdown）：板块概览 → 公司卡片 → 执行摘要（多子块 LLM）→ Step0b 快照 → Step2 分部/地理 → Step3 展望 → Step4 电话会 → Step5 新闻/并购/内部交易 → Step6 财务表。

**数据优先级（有 Bloomberg PG 时）**：分部/地理/内部人优先 `bloomberg_reader`，失败回退 FMP/SEC/画像；财务 `_get_validated_financials` 返回 `source_label` 为 `Bloomberg` 或 `FMP`。

**缓存**（改代码后若结果不变，先清缓存）：

| 层级 | 表 | 说明 |
|------|-----|------|
| 整份报告 | `sector_report_cache` | sector + 年 + 季 |
| 步骤片段 | `step_cache` | overview / step3 / step4 / exec_summary 等 |
| 画像 | `step_cache`（`__profile__`） | 按 ticker |
| 电话会 | `step_cache`（`__earnings__`） | 按 ticker + 季 |

清同一 sector 示例：

```bash
PYTHONPATH=src python3 -c "
from research_automation.core.database import get_connection
conn = get_connection()
conn.execute(\"DELETE FROM sector_report_cache WHERE sector='AI_Job_Replacement'\")
conn.execute(\"DELETE FROM step_cache WHERE sector='AI_Job_Replacement'\")
conn.commit(); conn.close()
print('done')
"
```

生成时传 `"force_refresh": true` 可跳过整份缓存。

---

## 7. 画像与电话会（内部服务）

无独立 HTTP；由行业报告生成时调用。

- **画像** `profile_service.get_profile`：SEC 10-K/20-F 节选 + LLM；失败回退 FMP profile。
- **电话会** `earnings_service`：逐字稿 FMP → EDGAR 8-K → sec-api.io；`analysis.quarter` 可能为 FMP 实际命中季度。

---

## 8. 环境变量

```bash
cp .env.example .env
```

| 变量 | 用途 |
|------|------|
| `ANTHROPIC_API_KEY` / `ANTHROPIC_MODEL` / `ANTHROPIC_MODEL_FINANCE` | Claude（优先） |
| `OPENAI_API_KEY` | LLM 回退 |
| `SEC_EDGAR_USER_AGENT` | SEC 访问（必填，含联系邮箱） |
| `SEC_API_KEY` | 可选，sec-api.io |
| `FMP_API_KEY` | 财务、电话会、内部交易等 |
| `DO_DB_*` / `BLOOMBERG_DB_ENABLED` | Bloomberg PostgreSQL 只读 |
| `BENZINGA_API_KEY` / `FINNHUB_API_KEY` | 公司新闻 |
| `NEWS_TIMEZONE` | 隔夜/昨日时间窗（默认 `America/New_York`） |
| `REPORT_RELEVANCE_THRESHOLD` | 行业报告新闻 relevance 下限（0–3） |
| `SECTOR_REPORT_STRICT_LLM` | `1` 时板块 LLM 失败不吞异常（调试） |

---

## 9. 安装与启动

**Python 3.11+**（开发多用 3.12）。

```bash
cd /path/to/project
python3 -m venv venv
source venv/bin/activate
pip install -r requirements.txt
cp .env.example .env
# 编辑 .env
```

启动 API（必须 `PYTHONPATH=src`）：

```bash
PYTHONPATH=src python3 -m uvicorn research_automation.main:app \
  --reload --reload-dir src --port 8000
```

- 使用 `--reload-dir src`，避免监视 `venv/` 导致反复重载。
- 文档：<http://127.0.0.1:8000/docs>
- 健康检查：<http://127.0.0.1:8000/health>

开发依赖与测试：

```bash
pip install -r requirements-dev.txt
PYTHONPATH=src pytest tests -q
PYTHONPATH=src pytest tests/test_openapi.py -q
```

---

## 10. 运维烟测

```bash
source venv/bin/activate

# OpenAPI 仅 4 个业务 path
PYTHONPATH=src pytest tests/test_openapi.py -q

# 板块列表
curl -s http://127.0.0.1:8000/api/v1/sector-reports/sectors

# 晨报（建议 include_classic_brief=false）
curl -s -m 300 "http://127.0.0.1:8000/api/v1/morning-brief?sector=Technology&include_classic_brief=false" | head -c 500

# 缓存探测
curl -s -o /dev/null -w "%{http_code}\n" \
  "http://127.0.0.1:8000/api/v1/sector-reports/Technology?probe_only=true"
```

Bloomberg / 财务 / Insider 等更深烟测命令见 `claude.md` 或历史 README 中的 Python one-liner（`bloomberg_reader`、`get_insider_summary`、`_get_validated_financials`）。

---

## 11. 局限与常见问题

| 类别 | 说明 |
|------|------|
| **晨报耗时** | 实时 RSS + 多 ticker 新闻 + 至少 2 次 LLM；无 bundle 级 HTTP 缓存 |
| **行业报告耗时** | 首次或 `force_refresh` 约 15～20 分钟；注意客户端超时 |
| **缓存** | 改 `sector_report_service` 后需清 `sector_report_cache` 与 `step_cache`，或 `force_refresh` |
| **Bloomberg** | 需 DO 网络可达；`BLOOMBERG_DB_ENABLED=0` 时全走 FMP/SEC |
| **FMP 电话会** | 部分 ticker/季度无稿；`analysis.quarter` 可能与请求季不同 |
| **CORS** | 前端源不在 `allow_origins` 时浏览器会拦请求 |
| **合规** | 输出仅供研究；遵守各数据源条款 |

- **ImportError**：确认 `PYTHONPATH=src`。
- **503 晨报**：查 `detail`、新闻 API 密钥与网络。
- **404 行业报告**：该 sector/季无缓存，需 POST 生成。
- **生成后内容仍旧**：清缓存或 `force_refresh: true`。

---

## 12. 合规

- SEC 数据访问规范：<https://www.sec.gov/os/accessing-edgar-data>
- 本项目输出**不构成投资建议**。
