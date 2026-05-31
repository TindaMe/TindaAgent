#!/bin/bash
SOURCE="$(cd "$(dirname "$0")" && pwd)"
case "${1:-}" in
    gateway) exec bash "$SOURCE/start.sh" "${@:2}" ;;
    --help|-h|help)
        echo "TindaAgent"
        echo "  tinda          启动 CLI"
        echo "  tinda gateway   启动 Web 服务"
        ;;
    *) cd "$SOURCE" && exec npm run tinda -- "$@" ;;
esac
