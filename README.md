# AI 投研系统（POC）

Python 全栈 POC：FastAPI 后端 + Streamlit 多页面前端，涵盖财务数据（yfinance → SQLite）、业务画像（LLM + 示例节选）、自动化晨报（RSS + LLM）。**数据仅供研究与联调，不构成投资建议。**

## 环境要求

- **Python 3.10+**
- **macOS / Linux**（Windows 亦可，注意路径与 `venv` 激活命令）
- 可访问外网（RSS、OpenAI、Yahoo 等）

## 1. 获取代码与虚拟环境

```bash
cd ai_research_system
python3 -m venv venv
source venv/bin/activate   # Windows: venv\Scripts\activate
pip install -r requirements.txt
```

## 2. 配置密钥

在项目根目录复制并编辑 `.env`（若没有则从 `.env.example` 参考）：

```env
OPENAI_API_KEY=sk-你的真实密钥
```

勿将填好密钥的 `.env` 提交到 Git。

## 3. 启动后端 API

**必须在项目根目录执行**，并带上 `PYTHONPATH`，以便加载 `src/research_automation`：

```bash
cd ai_research_system
source venv/bin/activate
PYTHONPATH=src uvicorn research_automation.main:app --reload --port 8000
```

- 接口文档：<http://127.0.0.1:8000/docs>
- 健康检查：<http://127.0.0.1:8000/health>

### （可选）准备财务库内数据

若「深度分析」财务表为空，可抓取并入库示例标的：

```bash
PYTHONPATH=src python tests/test_financials.py
```

## 4. 启动前端（Streamlit）

**新开一个终端**，同样在项目根：

```bash
cd ai_research_system
source venv/bin/activate
streamlit run app.py --server.port 8501
```

浏览器打开终端中提示的 Local URL（一般为 <http://localhost:8501>）。

- **深度分析**：查询财务 + 业务画像（带数据来源说明与对外链接）。
- **自动化晨报**：RSS + LLM 摘要；可点 **「刷新」** 重新拉取。

若本机 Streamlit 不支持 `--port` 短参数，可使用：

```bash
streamlit run app.py --server.port 8501
```

## 5. 常用调试命令

| 目的 | 命令 |
|------|------|
| 测 LLM | `PYTHONPATH=src python tests/test_llm.py` |
| 测财务抓取与库 | `PYTHONPATH=src python tests/test_financials.py` |

## 6. 数据溯源说明（摘要）

| 模块 | 主要来源 | 前端/API 行为 |
|------|-----------|----------------|
| 财务 | 本地 SQLite ← yfinance（Yahoo） | 返回 `data_source_label`、Yahoo 行情链接 |
| 业务画像 | 项目内示例节选 + OpenAI | 返回 `data_source_label`、SEC EDGAR 检索链接 |
| 晨报 | Reuters/Bloomberg 等 RSS + OpenAI | 各条尽量附带 `source_url` 阅读原文 |

摘要与分类由模型生成，**务必点击原文或法定披露链接核对**。

## 7. 项目结构（节选）

```text
ai_research_system/
├── app.py                 # Streamlit 入口（侧边栏导航）
├── frontend/pages/        # 深度分析、晨报等子页
├── src/research_automation/
│   ├── main.py            # FastAPI 入口
│   ├── api/v1/          # REST 路由
│   ├── core/database.py # SQLite + 财务读缓存
│   ├── extractors/      # yfinance、RSS、LLM
│   ├── services/        # 业务画像、晨报编排
│   └── models/          # Pydantic 契约
├── data/research.db      # SQLite（运行后生成）
├── requirements.txt
└── .env                  # 本地密钥（勿提交）
```

如有问题，先看后端终端报错与浏览器 **/docs** 中接口返回的 `detail` 字段。
