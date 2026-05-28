#!/usr/bin/env bash
# TindaAgent doctor (Linux/WSL, TypeScript runtime)

set -u

cd "$(cd "$(dirname "$0")" && pwd)"

if ! command -v node >/dev/null 2>&1; then
  echo "[ERROR] node not found"
  exit 127
fi

if [[ ! -d node_modules ]]; then
  npm install
fi

exec npm run doctor -- "$@"
