#!/bin/bash
# TindaAgent 快速安装 — 注册 tinda 命令到 PATH
SOURCE="$(cd "$(dirname "$0")" && pwd)"
BIN="$HOME/.local/bin"
mkdir -p "$BIN"

cat > "$BIN/tinda" << 'SCRIPT'
#!/bin/bash
SOURCE=/mnt/e/Python/release/source
case "${1:-}" in
    gateway) exec bash "$SOURCE/start.sh" "${@:2}" ;;
    --help|-h|help)
        echo "TindaAgent"
        echo "  tinda          启动 CLI"
        echo "  tinda gateway   启动 Web 服务"
        ;;
    *) cd "$SOURCE" && exec npm run tinda -- "$@" ;;
esac
SCRIPT

chmod +x "$BIN/tinda"
echo "done — tinda 已安装到 $BIN/tinda"
echo "试试: tinda --help"
