#!/bin/bash
# 双击运行（macOS）。首次会在本文件夹建一个独立 .venv 并装依赖，之后秒开。
cd "$(dirname "$0")"
if [ ! -d .venv ]; then
  echo "首次运行，正在创建环境并安装依赖（约 1-2 分钟）…"
  python3 -m venv .venv
fi
source .venv/bin/activate
python -m pip install -q --upgrade pip >/dev/null 2>&1
python -m pip install -q -r requirements.txt
exec python agent.py
