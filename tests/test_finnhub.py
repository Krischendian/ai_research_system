"""
Finnhub 公司新闻冒烟测试：需网络与 FINNHUB_API_KEY。在项目根执行：

    python tests/test_finnhub.py

会先加载项目根 ``.env``（与 uvicorn / llm_client 一致）；未写入 .env 时也可 ``export FINNHUB_API_KEY=...``。
"""
from __future__ import annotations

import os
import sys
from datetime import date, timedelta
from pathlib import Path

from dotenv import load_dotenv

_ROOT = Path(__file__).resolve().parents[1]
# 单独跑脚本时不会自动读 .env，须显式 load（shell 未 export 时仍可从文件取密钥）
load_dotenv(_ROOT / ".env", override=False)

_SRC = _ROOT / "src"
if str(_SRC) not in sys.path:
    sys.path.insert(0, str(_SRC))

from research_automation.extractors.finnhub_news import get_company_news  # noqa: E402


def main() -> None:
    if not (os.environ.get("FINNHUB_API_KEY") or "").strip():
        print(
            "跳过：未检测到 FINNHUB_API_KEY。"
            f"请在 {_ROOT / '.env'} 中设置 FINNHUB_API_KEY=...（无多余引号、无行首空格），"
            "或先 export 再运行。"
        )
        return

    end = date.today()
    start = end - timedelta(days=3)
    items = get_company_news("AAPL", start.isoformat(), end.isoformat())
    print(f"AAPL {start} ~ {end} 共 {len(items)} 条")
    for i, it in enumerate(items[:15], start=1):
        print(f"\n--- {i} ---")
        print("title:", it["title"][:120] + ("…" if len(it["title"]) > 120 else ""))
        print("datetime (NY):", it["datetime"])
        print("source:", it["source"])
        print("url:", (it["url"] or "")[:80])
        if it["summary"]:
            print("summary:", it["summary"][:200] + ("…" if len(it["summary"]) > 200 else ""))


if __name__ == "__main__":
    main()
