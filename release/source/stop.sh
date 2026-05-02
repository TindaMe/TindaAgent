#!/usr/bin/env bash
# TindaAgent 停止脚本 (Linux/WSL)

set -u

cd "$(cd "$(dirname "$0")" && pwd)"

MODE="${1:-}"
ARG="${2:-}"
PORTS_FILE="$PWD/.tinda_ports.list"
ENV_PORTS_VAR="TINDA_ACTIVE_PORTS"

PORT_LIST=""
PORT_RECORDS=""
PORT_RECORDS_OTHER=""
SCRIPT_ENV_TAG=""

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

print_usage() {
  echo "Usage:"
  echo "  $(basename "$0") --list"
  echo "  $(basename "$0") --port <port>"
  echo "  $(basename "$0") --all"
  echo "  $(basename "$0") --all-env"
}

is_numeric() {
  local v="${1:-}"
  [[ -n "$v" && "$v" =~ ^[0-9]+$ ]]
}

add_unique_port() {
  local p="${1:-}"
  if ! is_numeric "$p"; then
    return 0
  fi
  if (( p <= 0 || p > 65535 )); then
    return 0
  fi
  case " $PORT_LIST " in
    *" $p "*) ;;
    *) PORT_LIST="${PORT_LIST:+$PORT_LIST }$p" ;;
  esac
}

add_unique_record() {
  local rec="${1:-}"
  case " $PORT_RECORDS " in
    *" $rec "*) ;;
    *) PORT_RECORDS="${PORT_RECORDS:+$PORT_RECORDS }$rec" ;;
  esac
}

add_unique_other_record() {
  local rec="${1:-}"
  case " $PORT_RECORDS_OTHER " in
    *" $rec "*) ;;
    *) PORT_RECORDS_OTHER="${PORT_RECORDS_OTHER:+$PORT_RECORDS_OTHER }$rec" ;;
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

list_target_ports() {
  local include_env="${1:-0}"
  PORT_LIST=""
  PORT_RECORDS=""
  PORT_RECORDS_OTHER=""
  if [[ "${TINDA_PORTS_INCLUDE_ENV:-0}" == "1" ]]; then
    include_env="1"
  fi

  if [[ -f "$PORTS_FILE" ]]; then
    while IFS= read -r line || [[ -n "$line" ]]; do
      line="${line//$'\r'/}"
      line="${line//,/ }"
      line="${line//;/ }"
      for token in $line; do
        if parse_record_token "$token"; then
          if [[ "$REC_ENV" == "$SCRIPT_ENV_TAG" ]]; then
            add_unique_port "$REC_PORT"
            add_unique_record "${REC_ENV}:${REC_PORT}"
          elif [[ "$REC_ENV" == "legacy" ]]; then
            if is_local_listening_port "$REC_PORT"; then
              add_unique_port "$REC_PORT"
              # Legacy records are auto-migrated to current env once local ownership is confirmed.
              add_unique_record "${SCRIPT_ENV_TAG}:${REC_PORT}"
            else
              add_unique_other_record "${REC_ENV}:${REC_PORT}"
            fi
          else
            add_unique_other_record "${REC_ENV}:${REC_PORT}"
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
        add_unique_port "$REC_PORT"
        add_unique_record "${SCRIPT_ENV_TAG}:${REC_PORT}"
      fi
    done
  fi
}

write_ports_records_file() {
  local local_records="${1:-}"
  local foreign_records="${2:-}"
  : > "$PORTS_FILE"
  for rec in $local_records $foreign_records; do
    [[ -z "$rec" ]] && continue
    echo "$rec" >> "$PORTS_FILE"
  done
}

set_env_ports() {
  local list="${1:-}"
  local payload="$list"
  export "$ENV_PORTS_VAR=$list"
  if command -v cmd.exe >/dev/null 2>&1; then
    if [[ -z "$payload" ]]; then
      payload='""'
    fi
    cmd.exe /c setx "$ENV_PORTS_VAR" "$payload" >/dev/null 2>/dev/null || true
  fi
}

find_listen_pids() {
  local port="${1:-}"
  local out=""
  if command -v lsof >/dev/null 2>&1; then
    out="$(lsof -tiTCP:"$port" -sTCP:LISTEN 2>/dev/null | tr '\n' ' ' | xargs -r echo || true)"
  elif command -v ss >/dev/null 2>&1; then
    out="$(ss -ltnp "( sport = :$port )" 2>/dev/null | awk -F'pid=' 'NR>1 && NF>1 {split($2,a,","); print a[1]}' | sort -u | tr '\n' ' ' | xargs -r echo || true)"
  elif command -v netstat >/dev/null 2>&1; then
    out="$(netstat -ltnp 2>/dev/null | awk -v p=":$port" '$4 ~ p {split($7,a,"/"); if (a[1] ~ /^[0-9]+$/) print a[1]}' | sort -u | tr '\n' ' ' | xargs -r echo || true)"
  fi
  echo "$out"
}

remove_port() {
  local port="${1:-}"
  local new_list=""
  local kept_local_records=""
  local kept_foreign_records=""
  list_target_ports 0
  for p in $PORT_LIST; do
    if [[ "$p" != "$port" ]]; then
      new_list="${new_list:+$new_list }$p"
      kept_local_records="${kept_local_records:+$kept_local_records }${SCRIPT_ENV_TAG}:$p"
    fi
  done
  for rec in $PORT_RECORDS_OTHER; do
    local other_env="${rec%%:*}"
    local other_port="${rec##*:}"
    if [[ "$other_env" == "legacy" && "$other_port" == "$port" ]]; then
      continue
    fi
    kept_foreign_records="${kept_foreign_records:+$kept_foreign_records }$rec"
  done
  write_ports_records_file "$kept_local_records" "$kept_foreign_records"
  set_env_ports "$new_list"
}

stop_by_port() {
  local port="${1:-}"
  local killed=0
  local pid_file="$PWD/.tinda_server_${port}.pid"

  if [[ -f "$pid_file" ]]; then
    local pid_from_file=""
    pid_from_file="$(head -n 1 "$pid_file" 2>/dev/null || true)"
    if is_numeric "$pid_from_file"; then
      echo "[stop] try pid from pid file: $pid_from_file (port $port)"
      kill "$pid_from_file" >/dev/null 2>&1 || true
      sleep 0.2
      if kill -0 "$pid_from_file" >/dev/null 2>&1; then
        kill -9 "$pid_from_file" >/dev/null 2>&1 || true
      fi
      killed=1
    fi
  fi

  local pids=""
  pids="$(find_listen_pids "$port")"
  for pid in $pids; do
    if is_numeric "$pid"; then
      echo "[stop] kill listening pid $pid on port $port"
      kill "$pid" >/dev/null 2>&1 || true
      sleep 0.2
      if kill -0 "$pid" >/dev/null 2>&1; then
        kill -9 "$pid" >/dev/null 2>&1 || true
      fi
      killed=1
    fi
  done

  rm -f "$pid_file" >/dev/null 2>&1 || true
  remove_port "$port"

  if [[ "$killed" == "1" ]]; then
    echo "[stop] port $port processed"
  else
    echo "[stop] no process found for port $port"
  fi
}

list_ports() {
  list_target_ports 0
  if [[ -z "$PORT_LIST" && -z "$PORT_RECORDS_OTHER" ]]; then
    echo "[list] no tracked ports"
    return 0
  fi
  for p in $PORT_LIST; do
    local pids=""
    pids="$(find_listen_pids "$p")"
    if [[ -z "$pids" ]]; then
      echo "[list] port $p - stopped"
    else
      echo "[list] port $p - listening - pids $pids"
    fi
  done
  for rec in $PORT_RECORDS_OTHER; do
    local other_env="${rec%%:*}"
    local other_port="${rec##*:}"
    echo "[list] port $other_port - foreign-env($other_env) - skip-local-stop"
  done
}

stop_all() {
  local include_foreign="${1:-0}"
  list_target_ports 0
  if [[ -z "$PORT_LIST" && ( "$include_foreign" == "1" || -z "$PORT_RECORDS_OTHER" ) ]]; then
    echo "[stop] no tracked ports"
    write_ports_records_file "" ""
    set_env_ports ""
    return 0
  fi
  for p in $PORT_LIST; do
    stop_by_port "$p"
  done
  if [[ "$include_foreign" == "1" ]]; then
    write_ports_records_file "" ""
  else
    write_ports_records_file "" "$PORT_RECORDS_OTHER"
  fi
  echo "[stop] all tracked ports processed"
  list_target_ports 0
  set_env_ports "$PORT_LIST"
}

detect_env_tag

case "$MODE" in
  --help|-h)
    print_usage
    exit 0
    ;;
  --list)
    list_ports
    exit 0
    ;;
  --port)
    if [[ -z "$ARG" ]]; then
      echo "[ERROR] --port requires a number"
      exit 2
    fi
    if ! is_numeric "$ARG"; then
      echo "[ERROR] invalid port: $ARG"
      exit 2
    fi
    stop_by_port "$ARG"
    exit 0
    ;;
  --all)
    stop_all 0
    exit 0
    ;;
  --all-env)
    stop_all 1
    exit 0
    ;;
  "")
    print_usage
    exit 0
    ;;
  *)
    echo "[ERROR] unknown arg: $MODE"
    print_usage
    exit 2
    ;;
esac
