#!/usr/bin/env bash
# 错题收集工具 一键启动 (Linux / macOS)
# 双击或终端运行: bash 启动.sh
set -e
cd "$(dirname "$0")"
APP="tools/crop-tool/app.py"
VENV=".venv"

if [ ! -x "$VENV/bin/python" ]; then
  echo "[1/3] 首次运行，正在准备环境（约2分钟，仅一次）..."
  python3 -m venv "$VENV"
  "$VENV/bin/python" -m pip install -q --upgrade pip
  "$VENV/bin/python" -m pip install -q fastapi "uvicorn[standard]" python-multipart pillow opencv-python-headless numpy
fi

echo "[2/3] 启动服务..."
"$VENV/bin/python" "$APP" &
SERVER_PID=$!
sleep 2

echo "[3/3] 正在打开浏览器..."
( xdg-open http://localhost:8091 2>/dev/null || open http://localhost:8091 2>/dev/null ) || true
echo ""
echo "  工具已启动: http://localhost:8091"
echo "  关闭: 按 Ctrl+C"
wait $SERVER_PID
