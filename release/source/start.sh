#!/usr/bin/env bash
# TindaAgent starter (Linux/WSL)

set -euo pipefail

cd "$(cd "$(dirname "$0")" && pwd)"

usage() {
  echo "Usage:"
  echo "  $(basename "$0") [port] [port_retries] [host]"
  echo ""
  echo "Examples:"
  echo "  $(basename "$0")"
  echo "  $(basename "$0") 8000 20 0.0.0.0"
}

if [[ "${1:-}" == "--help" || "${1:-}" == "-h" ]]; then
  usage
  exit 0
fi

PORT="${1:-8000}"
PORT_RETRIES="${2:-20}"
HOST="${3:-0.0.0.0}"

is_uint() {
  local v="${1:-}"
  [[ -n "$v" && "$v" =~ ^[0-9]+$ ]]
}

if ! is_uint "$PORT" || (( PORT <= 0 || PORT > 65535 )); then
  echo "[ERROR] invalid port: $PORT"
  usage
  exit 2
fi

if ! is_uint "$PORT_RETRIES"; then
  echo "[ERROR] invalid port_retries: $PORT_RETRIES"
  usage
  exit 2
fi

PY_BIN=""
if command -v python3 >/dev/null 2>&1; then
  PY_BIN="python3"
elif command -v python >/dev/null 2>&1; then
  PY_BIN="python"
else
  echo "[ERROR] python not found (python3/python)"
  echo "[HINT] install python then rerun start.sh"
  exit 127
fi

if [[ ! -f "run_web.py" ]]; then
  echo "[ERROR] run_web.py not found in: $PWD"
  exit 2
fi

echo " TindaAgent 启动中..."
echo "   工作目录: $PWD"
echo "   监听地址: ${HOST}:${PORT}（起始端口，实际端口见 run_web.py 输出）"
echo "   端口重试: ${PORT_RETRIES} 次（占用时每次 +1）"
echo "   首端口等待: 1800ms（Ctrl+C 后优先复用起始端口）"
echo "   按 Ctrl+C 停止"
echo ""

# Keep wrapper transparent; run_web.py handles port retry/selection and prints final URL.
exec "$PY_BIN" run_web.py --host "$HOST" --port "$PORT" --port-retries "$PORT_RETRIES" --first-port-wait-ms 1800 --reload
