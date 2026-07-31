#!/bin/bash
# 双击运行（macOS）。首次会自动装依赖。
cd "$(dirname "$0")"
python3 -m pip install -r requirements.txt >/dev/null 2>&1 || python3 -m pip install --user -r requirements.txt
exec python3 agent.py
