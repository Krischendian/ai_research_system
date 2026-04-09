#!/usr/bin/env bash
# 在项目根目录执行：bash scripts/recreate_venv.sh
# 删除本地 ./venv 并按当前路径重建（修复从其它目录拷贝 venv 后出现的 bad interpreter）

set -euo pipefail
ROOT="$(cd "$(dirname "$0")/.." && pwd)"
cd "$ROOT"

echo "将在以下目录删除并重建虚拟环境: $ROOT/venv"
echo "（若 venv 是从其它机器/路径拷贝的，pip/python 常指向旧路径导致无法安装依赖）"
read -r -p "确认删除现有 venv 并继续? [y/N] " ans || true
if [[ ! "${ans:-}" =~ ^[Yy]$ ]]; then
  echo "已取消。"
  exit 1
fi

rm -rf venv
python3 -m venv venv
# shellcheck disable=SC1091
source venv/bin/activate
python -m pip install --upgrade pip
python -m pip install -r requirements.txt
echo ""
echo "完成。之后请执行: source venv/bin/activate"
echo "校验: python -c \"import earningscall; print('earningscall ok')\""
