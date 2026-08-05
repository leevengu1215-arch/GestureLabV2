#!/bin/bash
cd "$(dirname "$0")" || exit 1

export GESTURE_LAB_PORT=8766
echo "[Gesture Lab] 启动 PC 修复版 v3 独立审阅环境..."
echo "[Gesture Lab] 地址：http://127.0.0.1:8766/"
python3 app.py &
SERVER_PID=$!

for _ in {1..80}; do
  if curl -fsS http://127.0.0.1:8766/api/status >/dev/null 2>&1; then
    open http://127.0.0.1:8766/
    wait "$SERVER_PID"
    exit $?
  fi
  if ! kill -0 "$SERVER_PID" >/dev/null 2>&1; then
    wait "$SERVER_PID"
    read -r -p "审阅版启动失败，按 Return 关闭..."
    exit 1
  fi
  sleep 0.25
done

kill "$SERVER_PID" >/dev/null 2>&1
read -r -p "审阅版启动超时，按 Return 关闭..."
exit 1
