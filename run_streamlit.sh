#!/usr/bin/env bash
# 强制使用项目 venv，避免 zsh/别名把 python3 指到系统 Python 导致缺 plotly 等依赖。
set -euo pipefail
ROOT="$(cd "$(dirname "$0")" && pwd)"
cd "$ROOT"
PORT="${1:-8501}"
export PYTHONUNBUFFERED=1
echo "Starting Streamlit on port ${PORT} (imports can take 1–3 min on slow or synced disks; then you will see the Local URL)…" >&2
exec "$ROOT/venv/bin/python3" -m streamlit run "$ROOT/app.py" --server.port "$PORT"
