#!/usr/bin/env bash
# TindaAgent 启动脚本 (Linux/WSL)

set -u

PORT="${1:-8000}"
PORT_RETRIES="${2:-20}"
HOST="0.0.0.0"

echo " TindaAgent 启动中..."
echo "   起始地址: http://127.0.0.1:${PORT}"
echo "   端口重试: ${PORT_RETRIES} 次（占用时每次 +1）"
echo "   按 Ctrl+C 停止"
echo ""

cd "$(cd "$(dirname "$0")" && pwd)"
PY_BIN=""
if command -v python3 >/dev/null 2>&1; then
  PY_BIN="python3"
elif command -v python >/dev/null 2>&1; then
  PY_BIN="python"
else
  echo "错误: 未找到 Python 解释器（python3/python）"
  echo "请先安装 Python，或手动执行: python3 run_web.py --port $PORT --port-retries $PORT_RETRIES --host $HOST"
  exit 127
fi

# Use exec so Ctrl+C goes directly to uvicorn process and avoids wrapper parse artifacts.
exec "$PY_BIN" run_web.py --port "$PORT" --port-retries "$PORT_RETRIES" --host "$HOST"
