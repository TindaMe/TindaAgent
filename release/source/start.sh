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
ENTRY="dist/web/server.bundle.js"

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

if ! command -v node >/dev/null 2>&1; then
  echo "[ERROR] node not found"
  echo "[HINT] install Node.js 20+ then rerun start.sh"
  exit 127
fi

if [[ ! -f "$ENTRY" ]]; then
  echo "[INFO] $ENTRY not found; building TypeScript..."
  npm run build
fi

if [[ ! -f "$ENTRY" ]]; then
  echo "[ERROR] $ENTRY not found in: $PWD"
  exit 2
fi

echo " TindaAgent 启动中..."
echo "   工作目录: $PWD"
echo "   监听地址: ${HOST}:${PORT}"
echo "   端口重试: ${PORT_RETRIES} 次（当前 TS 入口使用起始端口）"
echo "   按 Ctrl+C 停止"
echo ""

HOST="$HOST" PORT="$PORT" PORT_RETRIES="$PORT_RETRIES" exec node "$ENTRY" --host="$HOST" --port="$PORT" --port-retries="$PORT_RETRIES"
