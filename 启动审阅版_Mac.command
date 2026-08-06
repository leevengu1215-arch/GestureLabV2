#!/bin/bash
cd "$(dirname "$0")" || exit 1

if ! command -v python3 >/dev/null 2>&1; then
  echo "[Gesture Lab] 未找到 Python 3。请先安装 Python 3.10 或更高版本。"
  read -r -p "按 Return 关闭..."
  exit 1
fi

python3 start_macos.py
