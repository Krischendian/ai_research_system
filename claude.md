# CLAUDE.md — AI 投研自动化项目上下文

> 每次新对话开头将此文件内容粘贴给 Claude，确保上下文完整。
> 更新原则：每次 session 结束前更新「当前状态」和「待解决问题」两节。

---

## 项目概述

**AI 投研自动化**：Python 项目，FastAPI 后端 + Streamlit 前端。
自动拉取公司财务数据、SEC 文件、电话会逐字稿、新闻，调用 LLM（优先 Anthropic Claude，回退 OpenAI）生成投研报告。

**仅用于研究与工程验证，不构成投资建议。**

---

## 技术栈

| 层 | 技术 |
|---|---|
| 后端 | Python + FastAPI，`PYTHONPATH=src uvicorn research_automation.main:app` |
| 前端 | Streamlit，`streamlit run app.py --server.port 8501` |
| 数据库 | SQLite `data/research.db`（主）；可选 PostgreSQL（Bloomberg 只读） |
| LLM | Anthropic Claude 优先（`ANTHROPIC_API_KEY`），回退 OpenAI |
| 数据源 | Bloomberg（可选）、FMP、SEC EDGAR、Benzinga/Finnhub、可选 Tavily |

---

## 模型配置（重要）

`.env` 当前配置：
```
ANTHROPIC_MODEL=claude-sonnet-4-6        # 轻量调用（晨报、搜索等）
ANTHROPIC_MODEL_FINANCE=claude-opus-4-7  # 行业报告所有 LLM 调用
```

**注意**：Step 0 板块概览 `_step_overview_sector` 特意去掉了 `task="finance"`，使用 Sonnet 而非 Opus。原因是 Opus 4.7 在生成中文长列表时会提前 `end_turn` 导致截断，Sonnet 更稳定。其他所有 Step 仍用 Opus（`task="finance"`）。

---

## 核心架构

```
Streamlit (app.py + frontend/pages/*.py)
    │
    ├── 行业报告页：直接 import sector_report_service（同进程，不过 HTTP）
    └── 其他页：HTTP → FastAPI 127.0.0.1:8000
                │
                ├── extractors/   fmp_client, sec_edgar, llm_client, bloomberg_reader…
                ├── services/     sector_report_service, profile_service, earnings_service…
                └── core/         database, company_manager, ticker_normalize…
                        │
                        └── SQLite data/research.db
```

---

## 主要功能模块

### 数据回退链（重要）
- **财务**：Bloomberg → FMP → SEC EDGAR
- **业务画像**：SEC 10-K/20-F → FMP profile（质量差时回退）
- **电话会逐字稿**：FMP → EDGAR 8-K → sec-api.io
- **业务分部收入**：Bloomberg PG_REVENUE → FMP Revenue Segmentation → 画像回退
- **地理收入**：Bloomberg PG_REVENUE(GEO) → FMP Geographic → 画像回退
- **Insider 交易**：Bloomberg INSIDER_MONTHLY_TRANSACTION → FMP → SEC EDGAR Form 4

### Bloomberg ETL（Windows bbg_etl.py）
- 运行环境：Windows，Bloomberg Terminal 已登录，`localhost:8194`
- 调度：每天 18:30 自动跑一次，或手动 `python -c "from bbg_etl import run_etl; run_etl()"`
- 写入目标：DO PostgreSQL `bloomberg` schema

**bloomberg schema 表一览：**

| 表名 | 内容 | 覆盖 |
|------|------|------|
| `securities` | 证券基础信息 | 30家 |
| `financials_annual` | 年度财务 | 30家×6年 |
| `financials_quarterly` | 季度财务 | 30家×12季 |
| `company_events` | 财报日期/股息 | 30家 |
| `earnings_transcripts` | 电话会逐字稿（Bloomberg PDF注入） | 部分 |
| `geo_revenue` | 地理收入分布（PG_REVENUE, GEO） | 30家×5年 |
| `segment_revenue` | 业务分部收入（PG_REVENUE, 默认） | 30家×5年 |
| `insider_monthly` | 内部人月度交易汇总 | 30家×12月 |
| `fetch_log` | ETL 执行日志 | - |

**Bloomberg API 已确认限制：**
- 电话会逐字稿全文：`refdata` 无全文字段，`tasvc` 是技术分析服务，逐字稿服务未找到
- 地理收入国家级细分：Segment ID 方式在 `refdata` 报 BAD_SEC，API 只能拿顶层地区
- 非美股 Insider 明细（个人级别）：字段无效，月度汇总可用
- BT/A、KBX 的 `gross_margin` 字段：Bloomberg `financials_annual` 无此数据（三年全 None），Step 6 显示 `—` 为正常行为

### bloomberg_reader.py（Mac 端只读）
提供以下函数，按 `internal_ticker` 查询，含 fallback 映射（FRE→FRE GY 等）：
- `get_security_info()`
- `get_financials_annual()`
- `get_financials_quarterly()`
- `get_earnings_transcript()`
- `get_geo_revenue()` — 地理收入，period_label 格式 "FY 2025"
- `get_segment_revenue()` — 业务分部，fiscal_year + segment_name + revenue(Millions)
- `get_insider_monthly()` — 月度交易汇总，已在 SQL 层过滤浮点 null（ABS < 1e-10）
- `is_data_fresh()`

### 行业六步报告
- 入口：`frontend/pages/04_SectorReport.py` 或 `scripts/generate_sector_report.py`
- 核心：`services/sector_report_service.py` → `generate_six_step_sector_report`
- 耗时：10～20 分钟+（force_refresh 更长）
- 缓存：`sector_report_cache`（整份）+ `step_cache`（step3/4/exec_summary/overview 等）
- **重要**：改代码后必须同时清 `sector_report_cache` 和对应 `step_cache`，否则新逻辑不生效

### Step 0 板块概览注意事项
- LLM 用 Sonnet（不用 `task="finance"`），避免 Opus 截断
- 有 ticker 验证重试（最多5次），确保19家公司全部出现
- `core_business` 截取长度已扩展至 300 字（原 150），给 LLM 更多具体信息
- 分组 prompt 强制要求列举①产品名称②客户行业③代表性数字，禁止使用"高附加值""综合解决方案"等空泛词汇
- 有 overview step 缓存，命中后不重新调用 LLM

### 非美股处理
- FMP symbol 映射表：`_FMP_TRANSCRIPT_TICKER_MAP`（如 `DHL GY` → `DHL.DE`）
- 映射表内标的：FMP 无稿时跳过 SEC，直接报 503
- **Session 8 新增**：季度回退逻辑——请求季度无数据时自动向前回退最多2个季度（BT/A、DHL、FRE 从 16/19 → 19/19）
- Bloomberg 数据：DHL GY、FRE GY、BT/A LN、KBX GY 均已有业务分部、地理收入、Insider 月度数据

### PDF 逐字稿注入
- 脚本：根目录 `inject_transcripts.py`
- 用途：将本地 Bloomberg PDF 写入 `step_cache`，绕过拉取链路
- 注意：`PDF_MAP` 中路径为本机绝对路径，换机器需修改

### Streamlit 渲染注意事项
- **KaTeX 问题**：`st.markdown` 默认开启 KaTeX，`$...$` 会被解读为 LaTeX inline math，导致金额如 `$2.2 billion` 被逐字符渲染。所有承载 LLM 输出的 `st.markdown` 调用必须走 `_safe_md()` 函数转义 `$`（已在 `04_SectorReport.py` 实现）。
- `st.session_state` 有内存缓存，清数据库后还需浏览器 Cmd+R 刷新。

### _filter_by_ticker_whitelist 注意事项
- **Session 8 已改为只过滤 `[TICKER]` 方括号格式**，裸全大写词（ARM/LEAP/NVIDIA 等）不再参与过滤
- 原因：裸词过滤需要维护 `_COMMON_ABBREVS` 白名单（打地鼠），每换板块都会出现新误伤词
- 漏网风险：若 LLM 不按规范写 `[TICKER]` 而写裸 ticker，会漏网；但 prompt 已强制 `[TICKER]` 前缀，实际概率极低
- 标题行（`###` 开头）被过滤时，其后续 bullet 也一并删除，避免孤立内容

---

## 已知问题与局限

| 问题 | 说明 |
|---|---|
| FMP 非美股电话会 | 部分季度/标的无数据，映射错误 symbol 得空结果 |
| Bloomberg 电话会逐字稿 | API 无全文字段，Desktop API 限制，暂无解 |
| Bloomberg 地理收入国家级 | API 只返回顶层地区（Americas/EMEA/APAC），国家级 Segment ID 方式报 BAD_SEC |
| Bloomberg 连接 | WiFi 网络封了 25060 端口，需用热点或将本机 IP 加入 DO Trusted Sources |
| Bloomberg 数据库为空误判 | `public` schema 为空，数据实际在 `bloomberg` schema，代码已正确指定 schema |
| BT/A、KBX 毛利率缺失 | Bloomberg `financials_annual` 无 gross_profit 字段，Step 6 显示 `—` 为正常行为，ETL 层待确认是否可补 |
| 报告生成无进度条 | 阻塞单线程，前端只有 spinner，开发时加 `PYTHONUNBUFFERED=1` |
| 缓存遮盖新逻辑 | 改代码后需手动清缓存或「强制刷新」才能看到效果；必须同时清 `sector_report_cache` 和 `step_cache` |
| iCloud 路径问题 | 项目不要放在 iCloud 同步目录，否则导入极慢 |
| SEC 频率限制 | User-Agent 必须合规，频率过高会被限 |
| Step 0 截断 | LLM（Sonnet）生成长列表时偶尔提前结束，有重试逻辑但不能100%保证。缓存命中后稳定。 |
| insider_monthly 浮点 null | Bloomberg 返回 ~-2.4e-14 作为 null 占位，ETL 已过滤，reader SQL 层也过滤（ABS < 1e-10） |
| 新闻抓取覆盖太少 | 很多公司 Benzinga 新闻为空，`signal_fetcher.py` 待排查 |
| ~~白名单误判品牌名~~ | 已解决：`_filter_by_ticker_whitelist` 改为只过滤 `[TICKER]` 格式，裸全大写词不再误判 |
| 板块汇总图最新季度偏低 | 最新季度部分公司未披露，汇总值偏低；已通过50%覆盖阈值过滤大部分断崖，但最后1-2个季度仍可能偏低 |

---

## 常用命令

```bash
# 启动后端
PYTHONPATH=src uvicorn research_automation.main:app --reload --port 8000

# 启动前端
streamlit run app.py --server.port 8501

# 清缓存（改代码后必须同时清这两个）
PYTHONPATH=src python3 -c "
from research_automation.core.database import get_connection
conn = get_connection()
conn.execute(\"DELETE FROM sector_report_cache\")
conn.execute(\"DELETE FROM step_cache WHERE step='step4'\")  # 按需指定 step
conn.commit()
conn.close()
print('done')
"

# 查看各 step 缓存行数
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

# 清画像缓存（单家公司）
PYTHONPATH=src python3 -c "
from research_automation.core.database import get_connection
conn = get_connection()
conn.execute(\"DELETE FROM step_cache WHERE sector='__profile__' AND step='business_profile' AND ticker='TICKER'\")
conn.commit(); conn.close()
"

# 清电话会缓存 + Step 3/4 渲染缓存
PYTHONPATH=src ./venv/bin/python3 -c "
from research_automation.core.database import get_connection
conn = get_connection()
cur1 = conn.execute(\"DELETE FROM step_cache WHERE sector='__earnings__'\")
cur2 = conn.execute(\"DELETE FROM step_cache WHERE step IN ('step3_per_company_outlook','step4_earning_call_section') OR step LIKE 'sector_report%'\")
print('cleared earnings:', cur1.rowcount, 'sector_report:', cur2.rowcount)
conn.commit(); conn.close()
"

# 测试 Bloomberg 读取
PYTHONPATH=src python3 -c "
from research_automation.extractors.bloomberg_reader import get_segment_revenue, get_insider_monthly, get_geo_revenue
print(get_segment_revenue('DHL'))
print(get_insider_monthly('BT/A'))
print(get_geo_revenue('IBM'))
"

# 测试 _get_validated_financials 数据来源
PYTHONPATH=src python3 -c "
from research_automation.services.sector_report_service import _get_validated_financials
rows, issues, src = _get_validated_financials('ACN', years=3)
print('来源:', src, '行数:', len(rows))
"

# 测试 Insider 汇总
PYTHONPATH=src python3 -c "
from research_automation.services.insider_service import get_insider_summary
s = get_insider_summary('BT/A LN', days_back=180)
print(s['source'], s['buy_count'], s['sell_count'])
"

# 测试分部数据入口
PYTHONPATH=src python3 -c "
from research_automation.services.sector_report_service import _get_revenue_segments
rows, year, source = _get_revenue_segments('DHL')
print(source, year, len(rows))
"

# 测试
PYTHONPATH=src pytest tests -q
```

---

## 架构决策记录

> 记录重要的"为什么这样做"，防止新对话重复讨论

| 决策 | 原因 |
|---|---|
| 行业报告页直接 import service，不过 HTTP | 避免长时间 HTTP 请求超时问题 |
| LLM 优先 Anthropic，回退 OpenAI | 成本与质量权衡，单一入口在 `llm_client.py` |
| FMP 回退画像时用专用短 prompt | FMP description 是营销文案，不适合完整 prompt，只抽 core_business |
| Step 0 用 Sonnet 不用 Opus | Opus 4.7 生成中文长列表时 end_turn 提前截断，Sonnet 更稳定 |
| Insider 窗口扩大到30天 | 默认7天窗口太短，FMP 最新记录可能在第8-30天，导致误判为无数据 |
| SEC Form 4 回退只对美股生效 | 非美股（GY/LN后缀）无 SEC 申报，自动跳过避免无效请求 |
| Bloomberg 分部/地理/Insider 优先 | 数据质量更高、覆盖非美股；FMP/SEC 作回退 |
| segment_revenue 过滤 <100M 分部 | Bloomberg 返回 Group Functions 等占位项（~5M），过滤后只保留实质业务线 |
| insider_monthly 月度数据作 Insider 汇总 | 非美股无 SEC Form 4，Bloomberg 月度汇总是唯一可用来源；美股有 FMP 细粒度数据时自然回退 |
| Step 3 max_tokens 提升至 3000 | 板块汇总19家公司内容量大，原1200不足导致截断 |
| `_safe_md()` 转义 $ 符号 | Streamlit KaTeX 把 `$...$` 解读为 LaTeX，导致金额逐字符渲染 |
| EBITDA<NI 校验加三段豁免 | SBC重SaaS（MDB）和大额税收抵免（ZM）会正常触发该规则，属误报；上界2×仍能截住真实数据错位 |
| Step 2 财年一致性改取 years=3 | ETL 写入下一财年 stub 后单取最新年会导致大面积误报，改为在多年候选中找与 step2_fy 同年的行 |
| FMP 逐字稿记录实际命中季度 | FMP 返回的季度可能早于请求季度，记录实际值避免参考来源标注有误 |
| _get_validated_financials 返回 source 标签 | Step 6 底注需要动态显示 Bloomberg/FMP，第三个返回值为 "Bloomberg" 或 "FMP" |
| Step 5 prompt 加覆盖硬约束 | LLM 自由发挥时会跳过部分公司，加 {coverage_tickers} 硬约束后覆盖率从5家提升至12+家 |
| Step 4 AI板块 max_tokens 1000→2000 | 16家公司每家平均只有62 token，远不够；扩到2000后覆盖率从5家提升至12+家 |
| Step 4 guard 放宽至"优先引用数字/原话，无则引用定性判断" | 原"必须有具体数字"过严，导致无精确数据的公司被排除 |
| 板块汇总图过滤覆盖<50%的季度 | 早期/最近季度数据不完整会造成断崖效应，50%阈值有效消除大部分伪断崖 |
| ROC 图截断用 p75 而非 p95 | 单期极值（如 +23×）会把 p95 撑开，p75 截断更激进，配合注释标出实际值 |
| Step 5 bullet 格式强制以 [TICKER] 开头 | 用公司名做小标题再在下面写 bullet 容易张冠李戴，[TICKER] 前缀让每条内容归属明确 |
| _filter_by_ticker_whitelist 标题行删除带走后续 bullet | 非白名单公司标题行被删后，孤立 bullet 会悬空；现在标题行删除时同步跳过后续内容直到下一标题 |
| _filter_by_ticker_whitelist 只过滤 [TICKER] 格式（Session 8）| 裸全大写词过滤需要维护 _COMMON_ABBREVS，每换板块都出现新误伤词（打地鼠）；改为只过滤方括号格式彻底解决 |
| earnings_service 非美股季度回退（Session 8）| BT/A、DHL、FRE 的 FMP 数据只有 2025Q4，请求 2026Q1 无数据时自动向前回退最多2个季度 |
| JLL 地理数据回退到 FY2021（Session 8）| FY2022-2025 全为浮点 null 占位值（Bloomberg 限制），拉5年数据后回退到最近有真实数据的年份 |

---

## 当前状态

- 项目结构稳定，行业六步报告 + 晨报功能基础完整
- 板块覆盖：AI_Job_Replacement (19家)，可扩展其他板块
- 上次更新：2026-05-13（Session 9）
- LLM 调用质量：Step 0 Sonnet + 其他 Step Opus，整体稳定
- 已完成本次 session：8 个前端报告问题全部修复，见 Session 9 总结

---

## 待解决问题

> 来源：2026-05-07 分析师评审会议 + Session 6/7 报告审查。优先级排序，完成后划掉。

### 🔴 高优先级

- [x] **Ticker 格式全局规范化**：已完成。
- [x] **Bloomberg 地理收入分布（Location）数据补全**：已完成。
- [x] **非美股公司数据缺失**：DHL GY、FRE GY、BT/A LN、KBX GY 收入结构已补全。

### 🟡 中优先级

- [x] **Step 0 分组描述太宏观**：已优化（core_business 扩至300字，prompt 强制列举产品名/客户行业）。
- [x] **Step 3 展望「原文未提及」过多**：已解决（19/19）。
- [x] **Step 4 AI 分析覆盖不全**：已大幅改善（5家→12+家），非美股电话会数据暂无解。
- [x] **Step 4 BT/A 被错标为非本板块标的**：已解决。
- [x] **Step 5 Insider 交易数据覆盖不足**：Bloomberg `insider_monthly` 已接入，16/19 家覆盖。
- [x] **Step 5 板块汇总覆盖不全**：prompt 加硬约束，覆盖率大幅提升。
- [x] **Step 5 张冠李戴 + 孤立 bullet**：prompt 强制 [TICKER] 前缀 + 过滤逻辑修复。
- [x] **数据来源标注统一**：Step 6 底注动态显示 Bloomberg/FMP，Step 0b 标注 Bloomberg/FMP 优先级。
- [ ] **新闻抓取覆盖太少**：很多公司 Benzinga 新闻为空，`signal_fetcher.py` 待排查；同时确认新闻只有标题时 LLM 是否会臆测。

### 🟠 低优先级

- [x] **Step 6 图表纵轴优化**：ROC 用 p75 截断 + 所有截断点加注释；板块汇总图过滤覆盖<50%季度消除断崖。
- [x] **板块总览「修正版」文字泄漏**：已修复。
- [ ] **BT/A、KBX 毛利率缺失**：Bloomberg `financials_annual` 无 `gross_profit` 字段（三年全 None）。在 Windows 端 `bbg_etl.py` 确认是否能拉 `GROSS_PROFIT` 字段；若 Bloomberg API 本身不提供则接受现状。
- [ ] **Step 0 截断根本解法**：Sonnet 仍有概率提前 end_turn，考虑改为代码生成分组（需给每家公司加 `group` 字段），不依赖 LLM。
- [x] **白名单误判品牌名**：已解决，`_filter_by_ticker_whitelist` 改为只过滤 `[TICKER]` 格式。

### 💡 数据源限制（无解，已确认）

- **Bloomberg RSS 命中率天然偏低**：是头条订阅（markets/technology/industries 三个 feed 各约30条，合计~90条），不是历史搜索 API。监控池里只有恰好成为 Bloomberg 头条的标的能命中。`_news_src_label` 自动按 `query_axis` 字段判断显示。
- **非美股电话会逐字稿**：Bloomberg Desktop API 无全文字段，FMP 覆盖有限，暂无解
- **Bloomberg 地理收入国家级细分**：Segment ID 在 `refdata` 报 BAD_SEC，API 只能拿顶层
- **非美股 Insider 个人级别数据**：Bloomberg 字段无效，只有月度汇总
- **BT/A、KBX 毛利率**：Bloomberg `financials_annual` 无此字段，待 ETL 层确认

---

## Session 交接记录

### 2026-05-13 Session 9 总结

**任务来源**：用户对 v32 报告提出 8 项前端展示问题。

**改动文件**：
- `services/sector_report_service.py`（主要）
- `services/profile_service.py`
- `models/company.py`
- `extractors/signal_fetcher.py`（前置改动）
- `frontend/pages/04_SectorReport.py`（无改动，验证后确认前端逻辑正确）

**8 个问题逐一修复**：

1. **数据覆盖表来源标签动态化**：`_step_overview_sector` 里 `_fin_sources_check` 同时收集 `_get_validated_financials` 和 profile 的 `data_source_label`，按 Bloomberg/FMP 情况动态显示 `Bloomberg Annual Financials` / `Bloomberg（优先）/ FMP（回退）` / `FMP Annual Financials`。

2. **核心主题 ACN 重复去重**：双层保险——prompt 加约束「每家公司最多一次，禁止空内容占位」+ 后处理 `_dedup_theme_bullets` 双遍扫描（pass1 统计 ticker 是否有实质内容，pass2 删除空且重复的条目）。

3. **Bloomberg RSS 新闻来源标签动态化**：`signal_fetcher` 里 Bloomberg RSS 已接入但下游标签写死 Benzinga；改为读 `query_axis` 字段动态推断 `_news_providers`，结果用 `_news_src_label` 替换三处硬编码字符串（Step 5 数据来源、执行摘要重要事件、参考来源表）。**说明**：Bloomberg RSS 是头条订阅（markets/technology/industries 各约30条，合计~90条），不是历史搜索 API，命中率天然偏低。

4. **管理层关键信号加精确来源**：`models/company.py` 的 `KeyManagementQuote` 加 `fmp_transcript_url` 字段；`profile_service.py` 合并 earnings_call 时填入 `_fmp_transcript_url(ticker, qlabel)` 构建的 URL（**注**：渲染层最终只保留来源文字，去掉 FMP 链接）。渲染层按 `data_source` 区分：electronics_call → `来源：Earning Call 逐字稿`；10-K → `来源：10-K {段落ID}`。

5. **执行摘要财务快照来源动态化**：与问题1同源，`_executive_summary` 函数内单独构建 `_es_fin_sources`，替换参考来源表里 `财务快照` 一行的来源标签。

6. **Step 3 展望每段加首句小标题**：`_one_company_block` 内对 `future_guidance` 和 `industry_view` 取首句截 20 字作小标题（无 LLM 调用，纯字符串处理）。

7. **来源详细化（去掉 FMP 链接）**：根据用户反馈"链接打不开、引文截半句无可读性"，把 Step 3 渲染里之前加的 `📎 [FMP逐字稿]` 全部移除，只保留 `来源：Earning Call 逐字稿（{qlabel}）` 文字。`profile_service` 里 `fmp_transcript_url` 字段保留（不渲染但写入数据，可未来再用）。

8. **Insider 格式统一**：无数据公司也显示 `- 买入：—` / `- 卖出：—` + 引用块说明可能原因，顶部加统一的 `*统计窗口：近 N 天　｜　数据来源：...*` 小注。

**额外发现并修复的问题**：

- **画像生成失败（AVY/CTSH/MDB/HCA）**：`profile_service.py` 里 `max_tokens=2000` 不够 CTSH 这类大公司的 JSON 输出。改为 `max_tokens=4000`（两处：line 602 和 1540；line 1678 续写仍为 1500）。
- **监控池外公司污染**：核心主题里出现 WPP/NKE/MKC/GIS/LEN/KBH/FDS 等其他板块公司。`_filter_non_sector_content` 之前只用于 `sec_event_text` 和 `sec_signal_text`，补加 `sec_theme_text` 和 `sec_risk_text` 两处过滤。同时修复正则只匹配纯字母 ticker 的问题（`[A-Z]{1,5}` → `[A-Z0-9/]{1,6}`，能识别 `[3M]`、`[BT/A]` 等含数字/斜杠 ticker）。
- **前端执行摘要 Tab 消失**：LLM 在 `sec_theme_text` 里自加了 `## 🔑 本季核心主题` H2 标题，破坏前端按 `##` 切 section 的逻辑。新增 `_strip_redundant_h2` 后处理，删除所有 `##\s+[🔑⚡💬⚠️🏆📊📋📂🏢]...` 模式的行，应用于全部 4 个执行摘要子板块文本。
- **Step 5 数据来源脚注混乱**：之前会拼出 `Bloomberg INSIDER_MONTHLY_TRANSACTION / Bloomberg（窗口内无交易）→ FMP / ...` 超长字符串。改为固定 `Bloomberg INSIDER_MONTHLY（优先）/ FMP / SEC Form 4（回退）` 简述。
- **API 余额耗尽事故**：v36/v37 报告执行摘要整段消失但无 Python 异常。根因是 Anthropic credit 用完，所有 LLM 调用返回 400 但被静默 fallback 为空字符串。充值后恢复正常。

**最终验证（v38 报告）**：FMP 链接 0 条 ✅、画像失败 0 家 ✅、监控池外公司 0 ✅、重复 H2 标题 0 ✅、执行摘要 5 个 Tab 子标题齐全 ✅、所有数据正确显示。

**未修复的低优先级问题**（用户决定不动）：
- Step 5 末尾 LLM 自加的 `## 重大投资与收购（续）` 孤立 H2 标题
- Step 5 续写部分大量空行
- 这两个问题仅影响视觉，不影响数据准确性

### 2026-05-12 Session 8 总结

**本次做了什么：对照分析师会议10条反馈逐条修复，并新增前端重构**

1. **`_filter_by_ticker_whitelist` 根本解法**
   - 改为只过滤 `[TICKER]` 方括号格式，裸全大写词（ARM/LEAP/KILIAN等）不再参与过滤
   - 彻底解决换板块需要手动维护 `_COMMON_ABBREVS` 的打地鼠问题
   - LEAP/ARM 等品牌名/项目代号在报告正文中不再显示 `〔非本板块标的〕`

2. **JLL 地理数据修复**
   - `get_geo_revenue` 改为拉5年数据，按年份降序找第一个有真实数据的年份
   - JLL FY2022-2025 全为浮点 null（Bloomberg 限制），自动回退到 FY2021 展示
   - 两处地理渲染代码（Step2 公司详情 + 公司简介卡片）均已修复

3. **非美股电话会季度回退（earnings_service.py）**
   - BT/A、DHL、FRE 请求 2026Q1 无数据，新增自动向前回退最多2个季度逻辑
   - Step 4 覆盖从 16/19 → 19/19
   - 数据验证：三家均成功命中 FY2025Q4

4. **Step 5 推断性分析过滤**
   - 发现 LLM 生成了"历史上此类信号与后续6至12个月股价表现存在一定正相关性"等投资建议性表述
   - prompt 加强：明确禁止数字编号分析小结、推断投资意义、"正相关""历史上"等分析师视角词汇
   - `_strip_step5_embedded_disclaimers` 增加过滤 `---` 分隔线（Streamlit 渲染为水平横线影响阅读）

5. **前端 `04_SectorReport.py` 完全重构**
   - TOC 目录（蓝色/绿色标签，点击跳转到对应 anchor）
   - 大标题（蓝色）：板块概览、执行摘要、Step 2-6 默认展开
   - 小标题（绿色）：各公司详情、业务简介 默认收缩
   - 执行摘要内部：黑色分类标题 + 总览段落 + 每条 bullet 拆成绿色小 expander
   - Step 4/5：板块总结按分类（AI部署/人员变动/营收/收购/战略合作）用黑色小标题分开
   - Step 6：季度图表 → 个股快速扫描 → 各公司财务详情折叠

**数据库层面待 ETL 端解决（Windows）：**
- DHL 地理数据：Bloomberg 只有 Worldwide，需补充 Location 路径
- JLL FY2022-2025：Bloomberg 写入浮点 null，需核查原因
- Insider 数据只到 2025年12月，需更新到 2026年3-4月

---

### 2026-05-12 Session 7 总结

**本次做了什么：处理会议反馈问题（对照2026-05-07分析师评审会议记录逐条修复）**

1. **Step 0 分组描述太宏观**
   - `core_business` 截取长度 150→300 字
   - prompt 格式要求改为"为[客户类型]提供[产品/服务名称]"，强制列举产品名、客户行业、代表性数字
   - 禁止词汇扩充（新增"全方位""一体化"）

2. **Step 4 AI部署覆盖不全**
   - `_s4_guard` 放宽：从"必须有数字"改为"优先引用数字，无则引用定性判断"
   - `s4_ai` prompt 加覆盖硬约束（每家有数据公司必须输出至少一条）
   - `s4_ai` max_tokens 1000→2000，`s4_fin` 800→1500，`s4_people` 800→1200
   - 覆盖率从 5家 提升至 12+家

3. **Step 4 BT/A 被错标**：已自动解决（Ticker 格式规范化后副作用消除）

4. **Step 5 张冠李戴 + 孤立 bullet**
   - prompt 强制要求每条 bullet 以 `[公司代码]` 开头，禁止用公司名做小标题
   - `_filter_by_ticker_whitelist` 新增标题行删除时联动删除后续 bullet 的逻辑

5. **Step 5 板块汇总覆盖**：加 `{coverage_tickers}` 硬约束，覆盖率大幅提升

6. **数据来源标注统一**
   - `_get_validated_financials` 新增第三返回值 source（"Bloomberg"/"FMP"）
   - Step 6 底注动态生成（Bloomberg/FMP/混合三种情况）
   - Step 0b 底注改为"Bloomberg / FMP Annual Financials（Bloomberg 优先）"

7. **Step 6 图表纵轴**
   - ROC 截断从 p95 改为 p75，截断点注释从"只标最大最小"改为"遍历所有截断点"
   - 板块汇总图新增覆盖阈值过滤（<50%公司有数据的季度不显示），消除断崖效应

8. **_COMMON_ABBREVS 扩充**：新增 NVIDIA/GOOGLE/APPLE/AMAZON/AZURE/OPENAI/NEXTGEN/BOOST/PARIS/FORD/ARM/LEAP/PRGP/KILIAN

**待下次继续：**
- 新闻抓取覆盖太少（`signal_fetcher.py` 排查 Benzinga 抓取逻辑）
- BT/A、KBX 毛利率：Windows ETL 确认 `GROSS_PROFIT` 字段

---

### 2026-05-11 Session 6 总结

**本次做了什么：全篇行业报告审查 + 14项 bug 修复**

1. **Bug #1 — 金额逐字符拆散（KaTeX）**：`04_SectorReport.py` 新增 `_safe_md()` 转义 `$`
2. **Bug #2 — Step 2 财年不一致警告误报**：`_get_validated_financials` 改拉 `years=3`
3. **Bug #3/#4 — 地理收入措辞 + HCA 口径标注**
4. **Bug #5/#6 — Step 3 截断 + profile_service 续写**：max_tokens 1200→3000，新增续写逻辑
5. **Bug #7 — JLL 展望内容缺失**：`profile_service.py` 新增内容质量校验
6. **Bug #8/#9 — Step 3 占位文字统一 + Step 4 参考来源裸数字**
7. **Bug #10 — Step 4 季度标注有误**：记录实际命中财季
8. **Bug #11 — TGT/RTO/EL/DG 新闻误抓**：`signal_fetcher.py` 新增精确关键词过滤
9. **Bug #12/#13 — Insider 金额措辞 + 白名单误伤**
10. **Bug #14 — Step 6 季度图表极值压缩**：纵轴 p95 截断 + 极值注释
11. **Bug #15 — BT/A、KBX 毛利率缺失**：确认为数据源限制
12. **Bug #16 — 财务校验 EBITDA<NI 误报**：新增三段豁免逻辑

---

### 2026-05-11 Session 5 总结

1. 缓存命中时 Step 2/5 自动刷新
2. Step 2 Bloomberg 数据正确进入报告（覆盖从 13/19 → 19/19）
3. 地理分布浮点 null 过滤
4. Step 2 来源标注动态化
5. Insider 来源链路透明化
6. `_load_per_company_signals_and_insiders` 统一调用 `get_insider_summary`

---

### 2026-05-10 Session 2 总结

Bloomberg ETL 新增三个数据域：`geo_revenue`、`insider_monthly`、`segment_revenue`。
`bloomberg_reader.py` 新增三个函数。报告接入 Bloomberg 优先逻辑。

---

### 2026-05-10 Session 1 总结

文字泄漏过滤、Step 0 稳定性改进、Step 5 SEC Form 4 回退、Step 6 图表纵轴动态范围、`ANTHROPIC_MODEL_FINANCE` 支持。

---

### 2026-05-09 Session 总结

Ticker 格式全局规范化、Step 3 展望覆盖修复（19/19）、Step 4 过滤逻辑修复（15/19）、Finnhub 路径 bug 修复。

---

### 2026-05-07 Session 总结

读取项目 README、生成 CLAUDE.md、根据会议反馈更新待解决问题清单。