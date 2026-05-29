#!/usr/bin/env bash
# TindaAgent status script (Linux/WSL)

set -u

cd "$(cd "$(dirname "$0")" && pwd)"

MODE="${1:---show}"
PORTS_FILE="$PWD/.tinda_ports.list"

TRACKED_PORTS=""
TRACKED_PORTS_FOREIGN=""
LISTEN_PORTS=""

declare -A LISTEN_PIDS_BY_PORT
SCRIPT_ENV_TAG=""

print_usage() {
  echo "Usage:"
  echo "  $(basename "$0") --show"
  echo "  $(basename "$0") --help"
}

detect_env_tag() {
  if [[ -n "${WSL_DISTRO_NAME:-}" ]]; then
    SCRIPT_ENV_TAG="wsl"
    return 0
  fi
  if [[ "${OS:-}" == "Windows_NT" ]]; then
    SCRIPT_ENV_TAG="windows"
    return 0
  fi
  if [[ -r /proc/version ]] && grep -qi microsoft /proc/version 2>/dev/null; then
    SCRIPT_ENV_TAG="wsl"
    return 0
  fi
  SCRIPT_ENV_TAG="linux"
}

normalize_env_tag() {
  local raw="${1:-}"
  raw="$(echo "$raw" | tr '[:upper:]' '[:lower:]')"
  case "$raw" in
    win|windows|nt) echo "windows" ;;
    wsl) echo "wsl" ;;
    linux|gnu/linux) echo "linux" ;;
    ""|legacy) echo "legacy" ;;
    *) echo "$raw" ;;
  esac
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

parse_record_token() {
  local token="${1:-}"
  local env_tag="legacy"
  local raw_port="$token"
  if [[ "$token" == *:* ]]; then
    local maybe_env="${token%%:*}"
    local maybe_port="${token#*:}"
    if [[ -n "$maybe_env" && -n "$maybe_port" ]]; then
      env_tag="$(normalize_env_tag "$maybe_env")"
      raw_port="$maybe_port"
    fi
  fi
  if ! is_numeric "$raw_port"; then
    return 1
  fi
  local p="$raw_port"
  if (( p <= 0 || p > 65535 )); then
    return 1
  fi
  REC_ENV="$env_tag"
  REC_PORT="$p"
  return 0
}

is_local_listening_port() {
  local p="${1:-}"
  local local_pids=""
  local_pids="$(find_listen_pids "$p")"
  [[ -n "$local_pids" ]]
}

add_unique_foreign_record() {
  local rec="${1:-}"
  case " $TRACKED_PORTS_FOREIGN " in
    *" $rec "*) ;;
    *) TRACKED_PORTS_FOREIGN="${TRACKED_PORTS_FOREIGN:+$TRACKED_PORTS_FOREIGN }$rec" ;;
  esac
}

collect_tracked_ports() {
  TRACKED_PORTS=""
  TRACKED_PORTS_FOREIGN=""
  local include_env="${TINDA_PORTS_INCLUDE_ENV:-0}"
  if [[ -f "$PORTS_FILE" ]]; then
    while IFS= read -r line || [[ -n "$line" ]]; do
      line="${line//$'\r'/}"
      line="${line//,/ }"
      line="${line//;/ }"
      for token in $line; do
        if parse_record_token "$token"; then
          if [[ "$REC_ENV" == "$SCRIPT_ENV_TAG" ]]; then
            add_unique_to_list TRACKED_PORTS "$REC_PORT"
          elif [[ "$REC_ENV" == "legacy" ]]; then
            if is_local_listening_port "$REC_PORT"; then
              add_unique_to_list TRACKED_PORTS "$REC_PORT"
            else
              add_unique_foreign_record "${REC_ENV}:${REC_PORT}"
            fi
          else
            add_unique_foreign_record "${REC_ENV}:${REC_PORT}"
          fi
        fi
      done
    done < "$PORTS_FILE"
  fi
  if [[ "$include_env" == "1" ]]; then
    local env_ports="${TINDA_ACTIVE_PORTS:-}"
    env_ports="${env_ports//$'\r'/}"
    env_ports="${env_ports//\"\"/ }"
    env_ports="${env_ports//,/ }"
    env_ports="${env_ports//;/ }"
    for token in $env_ports; do
      if parse_record_token "$token"; then
        add_unique_to_list TRACKED_PORTS "$REC_PORT"
      fi
    done
  fi
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
  [[ "$cmd" == *"dist/web/server.bundle.js"* || "$cmd" == *"dist/web/server.js"* || "$cmd" == *"src/web/server.ts"* ]]
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
      if [[ ! "$cmd" =~ ^node ]]; then
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

  if [[ -n "$TRACKED_PORTS_FOREIGN" ]]; then
    echo "[status] tracked-foreign: $TRACKED_PORTS_FOREIGN"
  else
    echo "[status] tracked-foreign: none"
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

detect_env_tag

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
