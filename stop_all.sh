#!/usr/bin/env bash
if [ -z "${BASH_VERSION:-}" ]; then
  exec bash "$0" "$@"
fi

set -euo pipefail

APP_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PACKAGE_MODE=0
if [[ -d "$APP_DIR/wheels" && -f "$APP_DIR/support/serve_frontend.py" ]]; then
  PACKAGE_MODE=1
fi

if [[ "$PACKAGE_MODE" -eq 1 ]]; then
  RUNTIME_DIR="$APP_DIR/.runtime"
else
  RUNTIME_DIR="$APP_DIR/.dev-runtime"
fi

BACKEND_PID_FILE="$RUNTIME_DIR/backend.pid"
FRONTEND_PID_FILE="$RUNTIME_DIR/frontend.pid"
LOCAL_PG_ROOT="$RUNTIME_DIR/postgres"
LOCAL_PG_DATA_DIR="$LOCAL_PG_ROOT/data"
LOCAL_PG_HOST="${FDV_LOCAL_PG_HOST:-127.0.0.1}"
LOCAL_PG_PORT="${FDV_LOCAL_PG_PORT:-55432}"

log() {
  printf '[stop_all] %s\n' "$*"
}

find_pg_command() {
  local command_name="$1"
  local bin_dir

  if command -v "$command_name" >/dev/null 2>&1; then
    command -v "$command_name"
    return 0
  fi

  for bin_dir in /usr/lib/postgresql/*/bin; do
    if [[ -x "$bin_dir/$command_name" ]]; then
      printf '%s\n' "$bin_dir/$command_name"
      return 0
    fi
  done

  return 1
}

pid_is_running() {
  local pid="$1"
  [[ -n "$pid" ]] && kill -0 "$pid" >/dev/null 2>&1
}

stop_process() {
  local pid="$1"

  if ! pid_is_running "$pid"; then
    return 0
  fi

  kill "$pid" >/dev/null 2>&1 || true
  for _ in $(seq 1 20); do
    if ! pid_is_running "$pid"; then
      return 0
    fi
    sleep 0.5
  done

  kill -9 "$pid" >/dev/null 2>&1 || true
}

process_belongs_to_app() {
  local pid="$1"
  local cwd

  cwd="$(readlink -f "/proc/$pid/cwd" 2>/dev/null || true)"
  [[ -n "$cwd" && "$cwd" == "$APP_DIR"* ]]
}

listening_pid_for_port() {
  local port="$1"

  if ! command -v ss >/dev/null 2>&1; then
    return 0
  fi

  ss -ltnp "( sport = :$port )" 2>/dev/null \
    | sed -nE 's/.*pid=([0-9]+).*/\1/p' \
    | head -n 1
}

stop_pid_file_process() {
  local name="$1"
  local pid_file="$2"

  if [[ ! -f "$pid_file" ]]; then
    return 1
  fi

  local pid
  pid="$(<"$pid_file")"
  stop_process "$pid"
  rm -f "$pid_file"
  log "${name} 已停止: pid=$pid"
  return 0
}

stop_port_process() {
  local name="$1"
  local port="$2"
  local pid

  pid="$(listening_pid_for_port "$port")"
  if [[ -z "$pid" ]]; then
    return 1
  fi

  if ! process_belongs_to_app "$pid"; then
    log "${name} 端口 $port 被外部进程占用，未处理: pid=$pid"
    return 1
  fi

  stop_process "$pid"
  log "${name} 已停止: pid=$pid"
  return 0
}

stop_service() {
  local name="$1"
  local pid_file="$2"
  local port="$3"

  if stop_pid_file_process "$name" "$pid_file"; then
    return 0
  fi

  if stop_port_process "$name" "$port"; then
    return 0
  fi

  log "${name} 未运行"
}

stop_local_postgres() {
  local pg_ctl_bin
  local pg_isready_bin

  if [[ ! -f "$LOCAL_PG_DATA_DIR/PG_VERSION" ]]; then
    return 0
  fi

  pg_ctl_bin="$(find_pg_command pg_ctl)" || return 0
  pg_isready_bin="$(find_pg_command pg_isready)" || return 0

  if ! "$pg_isready_bin" -h "$LOCAL_PG_HOST" -p "$LOCAL_PG_PORT" >/dev/null 2>&1; then
    return 0
  fi

  "$pg_ctl_bin" -D "$LOCAL_PG_DATA_DIR" stop -m fast >/dev/null 2>&1 || true
  log "本地 PostgreSQL 已停止: ${LOCAL_PG_HOST}:$LOCAL_PG_PORT"
}

main() {
  stop_service "前端服务" "$FRONTEND_PID_FILE" 5173
  stop_service "后端服务" "$BACKEND_PID_FILE" 8000
  stop_local_postgres
  log "清理完成"
}

main "$@"
