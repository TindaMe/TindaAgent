#!/usr/bin/env bash
# TindaAgent doctor (Linux/WSL)

set -u

cd "$(cd "$(dirname "$0")" && pwd)"

PY_BIN=""
if command -v python3 >/dev/null 2>&1; then
  PY_BIN="python3"
elif command -v python >/dev/null 2>&1; then
  PY_BIN="python"
else
  echo "[ERROR] python not found (python3/python)"
  exit 127
fi

exec "$PY_BIN" doctor.py "$@"
