#!/usr/bin/env bash
# TindaAgent 停止脚本 (Linux/WSL)

set -u

cd "$(cd "$(dirname "$0")" && pwd)"

MODE="${1:-}"
ARG="${2:-}"
PORTS_FILE="$PWD/.tinda_ports.list"
ENV_PORTS_VAR="TINDA_ACTIVE_PORTS"

PORT_LIST=""

print_usage() {
  echo "Usage:"
  echo "  $(basename "$0") --list"
  echo "  $(basename "$0") --port <port>"
  echo "  $(basename "$0") --all"
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

list_target_ports() {
  local include_env="${1:-1}"
  PORT_LIST=""

  if [[ -f "$PORTS_FILE" ]]; then
    while IFS= read -r line || [[ -n "$line" ]]; do
      line="${line//,/ }"
      line="${line//;/ }"
      for token in $line; do
        add_unique_port "$token"
      done
    done < "$PORTS_FILE"
  fi

  if [[ "$include_env" == "1" ]]; then
    local env_ports="${TINDA_ACTIVE_PORTS:-}"
    env_ports="${env_ports//,/ }"
    env_ports="${env_ports//;/ }"
    for token in $env_ports; do
      add_unique_port "$token"
    done
  fi
}

write_ports_file() {
  local list="${1:-}"
  : > "$PORTS_FILE"
  for p in $list; do
    echo "$p" >> "$PORTS_FILE"
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
  list_target_ports 0
  for p in $PORT_LIST; do
    if [[ "$p" != "$port" ]]; then
      new_list="${new_list:+$new_list }$p"
    fi
  done
  write_ports_file "$new_list"
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
  list_target_ports 1
  if [[ -z "$PORT_LIST" ]]; then
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
}

stop_all() {
  list_target_ports 1
  if [[ -z "$PORT_LIST" ]]; then
    echo "[stop] no tracked ports"
    write_ports_file ""
    set_env_ports ""
    return 0
  fi
  for p in $PORT_LIST; do
    stop_by_port "$p"
  done
  echo "[stop] all tracked ports processed"
  write_ports_file ""
  set_env_ports ""
}

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
    stop_all
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
