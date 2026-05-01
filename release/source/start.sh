#!/bin/bash
# TindaAgent 启动脚本 · 启动服务并打开浏览器
PORT="${1:-8000}"
HOST="0.0.0.0"

echo " TindaAgent 启动中..."
echo "   地址: http://127.0.0.1:${PORT}"
echo "   按 Ctrl+C 停止"
echo ""

# 打开浏览器（后台，忽略错误）
if command -v xdg-open &>/dev/null; then
    (sleep 1.5 && xdg-open "http://127.0.0.1:${PORT}") &
elif command -v open &>/dev/null; then
    (sleep 1.5 && open "http://127.0.0.1:${PORT}") &
elif command -v start &>/dev/null; then
    (sleep 1.5 && start "http://127.0.0.1:${PORT}") &
fi

cd "$(dirname "$0")"
python run_web.py --port "$PORT" --host "$HOST"
