#!/bin/bash
# TindaAgent 快速安装 — 注册 tinda 命令到 PATH
SOURCE="$(cd "$(dirname "$0")" && pwd)"
BIN="$HOME/.local/bin"
mkdir -p "$BIN"

if ! command -v realpath >/dev/null 2>&1; then
    echo "realpath not found; run ./tinda.sh from the project directory instead"
    exit 127
fi

TARGET="$(realpath --relative-to="$BIN" "$SOURCE/tinda.sh")"
ln -sfn "$TARGET" "$BIN/tinda"
echo "done — tinda 已安装到 $BIN/tinda"
echo "试试: tinda --help"
