#!/usr/bin/env bash
# TindaAgent status script (Linux/WSL)

set -u

cd "$(cd "$(dirname "$0")" && pwd)"

MODE="${1:---show}"
PORTS_FILE="$PWD/.tinda_ports.list"

TRACKED_PORTS=""
LISTEN_PORTS=""

declare -A LISTEN_PIDS_BY_PORT

print_usage() {
  echo "Usage:"
  echo "  $(basename "$0") --show"
  echo "  $(basename "$0") --help"
}

is_numeric() {
  local v="${1:-}"
  [[ -n "$v" && "$v" =~ ^[0-9]+$ ]]
}

add_unique_to_list() {
  local list_name="${1:-}"
  local p="${2:-}"
  if ! is_numeric "$p"; then
    return 0
  fi
  if (( p <= 0 || p > 65535 )); then
    return 0
  fi
  local cur="${!list_name:-}"
  case " $cur " in
    *" $p "*) ;;
    *) printf -v "$list_name" '%s' "${cur:+$cur }$p" ;;
  esac
}

contains_port() {
  local list="${1:-}"
  local p="${2:-}"
  case " $list " in
    *" $p "*) return 0 ;;
    *) return 1 ;;
  esac
}

add_listen_pair() {
  local port="${1:-}"
  local pid="${2:-}"
  if ! is_numeric "$port"; then
    return 0
  fi
  add_unique_to_list LISTEN_PORTS "$port"
  local cur="${LISTEN_PIDS_BY_PORT[$port]:-}"
  case " $cur " in
    *" $pid "*) ;;
    *) LISTEN_PIDS_BY_PORT["$port"]="${cur:+$cur }$pid" ;;
  esac
}

collect_tracked_ports() {
  TRACKED_PORTS=""
  if [[ -f "$PORTS_FILE" ]]; then
    while IFS= read -r line || [[ -n "$line" ]]; do
      line="${line//,/ }"
      line="${line//;/ }"
      for token in $line; do
        add_unique_to_list TRACKED_PORTS "$token"
      done
    done < "$PORTS_FILE"
  fi
  local env_ports="${TINDA_ACTIVE_PORTS:-}"
  env_ports="${env_ports//,/ }"
  env_ports="${env_ports//;/ }"
  for token in $env_ports; do
    add_unique_to_list TRACKED_PORTS "$token"
  done
}

is_agent_pid() {
  local pid="${1:-}"
  if ! is_numeric "$pid"; then
    return 1
  fi
  if [[ ! -r "/proc/$pid/cmdline" ]]; then
    return 1
  fi
  local cmd
  cmd="$(tr '\0' ' ' < "/proc/$pid/cmdline" 2>/dev/null || true)"
  [[ "$cmd" == *"run_web.py"* || "$cmd" == *"uvicorn"* || "$cmd" == *"TindaAgent.Web.server"* || "$cmd" == *"Web.server"* ]]
}

collect_listening_ports() {
  LISTEN_PORTS=""
  LISTEN_PIDS_BY_PORT=()
  if command -v ss >/dev/null 2>&1; then
    while IFS= read -r line || [[ -n "$line" ]]; do
      [[ -z "$line" ]] && continue
      local_addr="$(awk '{print $4}' <<<"$line")"
      if [[ ! "$local_addr" =~ :([0-9]+)$ ]]; then
        continue
      fi
      port="${BASH_REMATCH[1]}"
      pids="$(grep -oE 'pid=[0-9]+' <<<"$line" | cut -d= -f2 | sort -u | tr '\n' ' ')"
      for pid in $pids; do
        if is_agent_pid "$pid"; then
          add_listen_pair "$port" "$pid"
        fi
      done
    done < <(ss -ltnpH 2>/dev/null || true)
    return 0
  fi
  if command -v lsof >/dev/null 2>&1; then
    while IFS= read -r line || [[ -n "$line" ]]; do
      cmd="$(awk '{print $1}' <<<"$line")"
      pid="$(awk '{print $2}' <<<"$line")"
      addr="$(awk '{print $9}' <<<"$line")"
      if [[ ! "$cmd" =~ ^(python|python3|uvicorn) ]]; then
        continue
      fi
      if [[ ! "$addr" =~ :([0-9]+)$ ]]; then
        continue
      fi
      port="${BASH_REMATCH[1]}"
      if is_agent_pid "$pid"; then
        add_listen_pair "$port" "$pid"
      fi
    done < <(lsof -nP -iTCP -sTCP:LISTEN 2>/dev/null | tail -n +2 || true)
  fi
}

show_status() {
  collect_tracked_ports
  collect_listening_ports

  if [[ -z "$TRACKED_PORTS" ]]; then
    echo "[status] tracked: none"
  else
    echo "[status] tracked: $TRACKED_PORTS"
  fi

  if [[ -z "$LISTEN_PORTS" ]]; then
    echo "[status] listening(agent): none"
  else
    echo "[status] listening(agent): $LISTEN_PORTS"
    for p in $LISTEN_PORTS; do
      echo "[listen] port $p - pids ${LISTEN_PIDS_BY_PORT[$p]}"
    done
  fi

  orphan=""
  for p in $TRACKED_PORTS; do
    if ! contains_port "$LISTEN_PORTS" "$p"; then
      orphan="${orphan:+$orphan }$p"
    fi
  done

  untracked=""
  for p in $LISTEN_PORTS; do
    if ! contains_port "$TRACKED_PORTS" "$p"; then
      untracked="${untracked:+$untracked }$p"
    fi
  done

  if [[ -z "$orphan" ]]; then
    echo "[status] orphan-tracked: none"
  else
    echo "[status] orphan-tracked: $orphan"
  fi

  if [[ -z "$untracked" ]]; then
    echo "[status] untracked-listening: none"
  else
    echo "[status] untracked-listening: $untracked"
  fi
}

case "$MODE" in
  --show)
    show_status
    ;;
  --help|-h)
    print_usage
    ;;
  *)
    echo "[ERROR] unknown arg: $MODE"
    print_usage
    exit 2
    ;;
esac
