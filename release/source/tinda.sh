#!/bin/bash
SOURCE=/mnt/e/Python/release/source
export PYTHONPATH="$SOURCE:$PYTHONPATH"
case "${1:-}" in
    gateway) exec bash "$SOURCE/start.sh" "${@:2}" ;;
    --help|-h|help)
        echo "TindaAgent"
        echo "  tinda          启动 CLI"
        echo "  tinda gateway   启动 Web 服务"
        ;;
    *) cd "$SOURCE" && exec python3 -m TindaAgent.CLI.main "$@" ;;
esac
