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

RUNTIME_VENV_DIR="$RUNTIME_DIR/venv"
RUNTIME_MARKER_FILE="$RUNTIME_DIR/runtime-mode.txt"
BACKEND_PID_FILE="$RUNTIME_DIR/backend.pid"
FRONTEND_PID_FILE="$RUNTIME_DIR/frontend.pid"
DATABASE_INIT_LOG="$RUNTIME_DIR/database-init.log"
RUNTIME_INSTALL_LOG="$RUNTIME_DIR/runtime-install.log"
BACKEND_HOST="${APP_BACKEND_HOST:-127.0.0.1}"
BACKEND_PORT="${APP_BACKEND_PORT:-8000}"
FRONTEND_HOST="${APP_FRONTEND_HOST:-127.0.0.1}"
FRONTEND_PORT="${APP_FRONTEND_PORT:-5173}"
BACKEND_BASE_URL="http://${BACKEND_HOST}:${BACKEND_PORT}"
BACKEND_HEALTH_URL="${BACKEND_BASE_URL}/health"
FRONTEND_URL="http://${FRONTEND_HOST}:${FRONTEND_PORT}/"
FRONTEND_DIST_DIR="$APP_DIR/frontend/dist"
LOCAL_PG_ROOT="$RUNTIME_DIR/postgres"
LOCAL_PG_DATA_DIR="$LOCAL_PG_ROOT/data"
LOCAL_PG_SOCKET_DIR="$LOCAL_PG_ROOT/socket"
LOCAL_PG_LOG="$LOCAL_PG_ROOT/postgres.log"
LOCAL_PG_HOST="${APP_LOCAL_PG_HOST:-127.0.0.1}"
LOCAL_PG_PORT="${APP_LOCAL_PG_PORT:-55432}"
LOCAL_PG_DB="${APP_LOCAL_PG_DB:-trader}"
LOCAL_PG_USER="${APP_LOCAL_PG_USER:-${USER:-$(id -un)}}"
BROWSER_OPEN_LOG="$RUNTIME_DIR/browser-open.log"
PYTHON_BIN=""

mkdir -p "$RUNTIME_DIR"

log() {
  printf '[start_all] %s\n' "$*"
}

require_command() {
  local command_name="$1"
  if ! command -v "$command_name" >/dev/null 2>&1; then
    printf '[start_all] 缺少命令: %s\n' "$command_name" >&2
    exit 1
  fi
}

# 项目要求 Python >= 3.12（见 pyproject.toml `requires-python`），但
# 在 macOS 上 `python3` 默认指向系统自带的 3.9，会让后续 `pip install -e .`
# 因为 PEP 660 editable 支持缺失而失败。这里优先选择显式版本号；只有
# 都不在 PATH 时才退化到 `python3`，并且仍要求其版本不低于 3.12。
select_python_interpreter() {
  local candidates=(python3.14 python3.13 python3.12)
  for candidate in "${candidates[@]}"; do
    if command -v "$candidate" >/dev/null 2>&1; then
      printf '%s' "$candidate"
      return 0
    fi
  done
  if command -v python3 >/dev/null 2>&1 \
      && python3 -c 'import sys; sys.exit(0 if sys.version_info >= (3, 12) else 1)' 2>/dev/null; then
    printf 'python3'
    return 0
  fi
  printf '[start_all] 未找到满足 >=3.12 的 Python (依次尝试 %s 与 python3)\n' "${candidates[*]}" >&2
  exit 1
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

load_env_file() {
  local env_file="$1"

  if [[ ! -f "$env_file" ]]; then
    return 0
  fi

  log "加载配置: $env_file"
  set -a
  # shellcheck disable=SC1090
  source "$env_file"
  set +a
}

load_env() {
  if [[ -n "${APP_ENV_FILE:-}" ]]; then
    load_env_file "$APP_ENV_FILE"
    return 0
  fi

  if [[ -f "$APP_DIR/config/.env" ]]; then
    load_env_file "$APP_DIR/config/.env"
  fi

  if [[ -f "$APP_DIR/.env" ]]; then
    load_env_file "$APP_DIR/.env"
  fi
}

python_run() {
  "$PYTHON_BIN" "$@"
}

ensure_runtime_python() {
  local expected_mode
  local install_target

  local python_interpreter
  python_interpreter="$(select_python_interpreter)"
  expected_mode="repo"
  if [[ "$PACKAGE_MODE" -eq 1 ]]; then
    expected_mode="bundle"
  fi

  if [[ -x "$RUNTIME_VENV_DIR/bin/python" && -f "$RUNTIME_MARKER_FILE" ]]; then
    if [[ "$(<"$RUNTIME_MARKER_FILE")" == "$expected_mode" ]]; then
      PYTHON_BIN="$RUNTIME_VENV_DIR/bin/python"
      return 0
    fi
  fi

  rm -rf "$RUNTIME_VENV_DIR"
  mkdir -p "$RUNTIME_DIR"
  : > "$RUNTIME_INSTALL_LOG"

  log "准备 Python 运行环境 (${python_interpreter})"
  "$python_interpreter" -m venv "$RUNTIME_VENV_DIR" >>"$RUNTIME_INSTALL_LOG" 2>&1

  if [[ "$PACKAGE_MODE" -eq 1 ]]; then
    install_target="本地 wheels"
    "$RUNTIME_VENV_DIR/bin/python" -m pip install \
      --no-index \
      --find-links "$APP_DIR/wheels" \
      polymarket-trader \
      >>"$RUNTIME_INSTALL_LOG" 2>&1
  else
    install_target="当前仓库"
    "$RUNTIME_VENV_DIR/bin/python" -m pip install -e "$APP_DIR" >>"$RUNTIME_INSTALL_LOG" 2>&1
  fi

  printf '%s\n' "$expected_mode" > "$RUNTIME_MARKER_FILE"
  PYTHON_BIN="$RUNTIME_VENV_DIR/bin/python"
  log "Python 运行环境已就绪 (${install_target})"
}

http_ready() {
  local url="$1"
  python_run - "$url" <<'PY'
import sys
import urllib.request

url = sys.argv[1]
try:
    with urllib.request.urlopen(url, timeout=1.5) as response:
        sys.exit(0 if 200 <= response.status < 500 else 1)
except Exception:
    sys.exit(1)
PY
}

wait_for_http() {
  local name="$1"
  local url="$2"
  local timeout_seconds="${3:-30}"
  local attempts=$((timeout_seconds * 2))

  for _ in $(seq 1 "$attempts"); do
    if http_ready "$url"; then
      return 0
    fi
    sleep 0.5
  done

  log "$name 启动失败"
  return 1
}

start_background_process() {
  local command_text="$1"
  local pid_file="$2"
  # 用 pid_file 派生日志名(backend.pid → backend.log),保留 stdout/stderr 到文件;
  # 此前丢 /dev/null 让启动失败完全黑箱、无法 debug——startup 死了找不到原因。
  local log_file="${pid_file%.pid}.log"

  # macOS 默认不带 setsid(Linux util-linux),用 nohup + disown 跨平台等价:
  # nohup 忽略 SIGHUP(终端关闭信号),disown 从 shell 作业表移除,效果同 setsid
  # 创建新会话——子进程在终端关闭后继续存活。
  if command -v setsid >/dev/null 2>&1; then
    setsid bash -c "cd '$APP_DIR' && exec $command_text" </dev/null >"$log_file" 2>&1 &
  else
    nohup bash -c "cd '$APP_DIR' && exec $command_text" </dev/null >"$log_file" 2>&1 &
  fi
  local launched_pid=$!
  echo "$launched_pid" > "$pid_file"
  disown "$launched_pid" 2>/dev/null || true
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

cleanup_pid_file_process() {
  local pid_file="$1"

  if [[ ! -f "$pid_file" ]]; then
    return 0
  fi

  local pid
  pid="$(<"$pid_file")"
  stop_process "$pid"
  rm -f "$pid_file"
}

listening_pid_for_port() {
  local port="$1"

  if command -v ss >/dev/null 2>&1; then
    ss -ltnp "( sport = :$port )" 2>/dev/null \
      | sed -nE 's/.*pid=([0-9]+).*/\1/p' \
      | head -n 1
  elif command -v lsof >/dev/null 2>&1; then
    lsof -ti ":$port" -sTCP:LISTEN 2>/dev/null | head -n 1
  fi
}

process_belongs_to_app() {
  local pid="$1"
  local cwd

  if [[ -r "/proc/$pid/cwd" ]]; then
    cwd="$(readlink -f "/proc/$pid/cwd" 2>/dev/null || true)"
  elif command -v lsof >/dev/null 2>&1; then
    cwd="$(lsof -p "$pid" -a -d cwd -Fn 2>/dev/null | sed -n 's/^n//p' | head -n 1)"
  fi
  [[ -n "$cwd" && "$cwd" == "$APP_DIR"* ]]
}

process_matches_service() {
  local pid="$1"
  local command_fragment="$2"
  local cmdline

  if [[ -r "/proc/$pid/cmdline" ]]; then
    cmdline="$(tr '\0' ' ' <"/proc/$pid/cmdline" 2>/dev/null || true)"
  else
    cmdline="$(ps -p "$pid" -o command= 2>/dev/null || true)"
  fi
  [[ -n "$cmdline" && "$cmdline" == *"$command_fragment"* ]]
}

reclaim_managed_port() {
  local port="$1"
  local service_name="$2"
  local command_fragment="$3"
  local pid

  pid="$(listening_pid_for_port "$port")"
  if [[ -z "$pid" ]]; then
    return 0
  fi

  if process_belongs_to_app "$pid"; then
    log "停止占用 $port 的已有${service_name}: pid=$pid"
    stop_process "$pid"
    return 0
  fi

  if process_matches_service "$pid" "$command_fragment"; then
    log "停止占用 $port 的其他${service_name}: pid=$pid"
    stop_process "$pid"
    return 0
  fi

  log "${service_name} 端口 $port 已被外部进程占用: pid=$pid"
  return 1
}

stop_existing_services() {
  log "关闭已有前后端服务"
  cleanup_pid_file_process "$FRONTEND_PID_FILE"
  cleanup_pid_file_process "$BACKEND_PID_FILE"
  reclaim_managed_port "$FRONTEND_PORT" "前端服务" "serve_frontend.py" || return 1
  reclaim_managed_port "$BACKEND_PORT" "后端服务" "polymarket_trader.api.app:create_app" || return 1
  # 兜底:按名再扫一遍,捕获 pid 文件丢失或端口已被释放但进程还在的僵尸——
  # 这种残留进程会和新启动实例抢资源(DB 连接/账户位/WS slot),实盘可能双发。
  # || true:pkill 无匹配返回 1,不算错误。
  pkill -f "uvicorn polymarket_trader" 2>/dev/null || true
  pkill -f "serve_frontend.py" 2>/dev/null || true
  sleep 0.3
}

initialize_database_once() {
  : > "$DATABASE_INIT_LOG"
  if python_run - <<'PY' >>"$DATABASE_INIT_LOG" 2>&1
import asyncio
from polymarket_trader.config import Settings
from polymarket_trader.infra.db import initialize_database

settings = Settings()
asyncio.run(initialize_database(settings.database_url))
PY
  then
    return 0
  fi

  return 1
}

database_config_is_default() {
  local database_url="${DATABASE_URL:-}"
  local database_driver="${DATABASE_DRIVER:-postgresql+asyncpg}"
  local database_host="${DATABASE_HOST:-localhost}"
  local database_port="${DATABASE_PORT:-5432}"
  local database_name="${DATABASE_NAME:-trader}"
  local database_user="${DATABASE_USER:-trader}"
  local database_password="${DATABASE_PASSWORD:-}"

  [[ -z "$database_url" ]] || return 1
  [[ -z "$database_password" ]] || return 1
  [[ "$database_driver" == "postgresql+asyncpg" ]] || return 1
  [[ "$database_host" == "localhost" ]] || return 1
  [[ "$database_port" == "5432" ]] || return 1
  [[ "$database_name" == "trader" ]] || return 1
  [[ "$database_user" == "trader" ]] || return 1
}

database_url_targets_local_bootstrap_postgres() {
  local database_url="${DATABASE_URL:-}"

  [[ -n "$database_url" ]] || return 1

  DATABASE_URL_TO_CHECK="$database_url" \
  LOCAL_PG_HOST_TO_CHECK="$LOCAL_PG_HOST" \
  LOCAL_PG_PORT_TO_CHECK="$LOCAL_PG_PORT" \
  LOCAL_PG_DB_TO_CHECK="$LOCAL_PG_DB" \
  python3 - <<'PY'
import os
import sys
from urllib.parse import urlsplit

database_url = os.environ["DATABASE_URL_TO_CHECK"]
expected_host = os.environ["LOCAL_PG_HOST_TO_CHECK"]
expected_port = int(os.environ["LOCAL_PG_PORT_TO_CHECK"])
expected_db = os.environ["LOCAL_PG_DB_TO_CHECK"]

try:
    parsed = urlsplit(database_url)
except Exception:
    sys.exit(1)

path = parsed.path.lstrip("/")
port = parsed.port
host = parsed.hostname

if host == expected_host and port == expected_port and path == expected_db:
    sys.exit(0)
sys.exit(1)
PY
}

bootstrap_local_postgres() {
  local initdb_bin
  local pg_ctl_bin
  local createdb_bin
  local pg_isready_bin
  local psql_bin

  initdb_bin="$(find_pg_command initdb)" || {
    log "未找到 initdb，无法自动拉起本地 PostgreSQL"
    return 1
  }
  pg_ctl_bin="$(find_pg_command pg_ctl)" || {
    log "未找到 pg_ctl，无法自动拉起本地 PostgreSQL"
    return 1
  }
  createdb_bin="$(find_pg_command createdb)" || {
    log "未找到 createdb，无法自动创建本地开发库"
    return 1
  }
  pg_isready_bin="$(find_pg_command pg_isready)" || {
    log "未找到 pg_isready，无法探测本地 PostgreSQL 状态"
    return 1
  }
  psql_bin="$(find_pg_command psql)" || {
    log "未找到 psql，无法校验本地开发库"
    return 1
  }

  mkdir -p "$LOCAL_PG_ROOT" "$LOCAL_PG_SOCKET_DIR"

  if [[ ! -f "$LOCAL_PG_DATA_DIR/PG_VERSION" ]]; then
    log "初始化仓库内 PostgreSQL 数据目录"
    "$initdb_bin" \
      -D "$LOCAL_PG_DATA_DIR" \
      -U "$LOCAL_PG_USER" \
      -A trust \
      --auth-host=trust \
      --auth-local=trust \
      >/dev/null
  fi

  if "$pg_isready_bin" -h "$LOCAL_PG_HOST" -p "$LOCAL_PG_PORT" >/dev/null 2>&1; then
    log "复用本地 PostgreSQL: ${LOCAL_PG_HOST}:$LOCAL_PG_PORT"
  else
    log "启动仓库内 PostgreSQL: ${LOCAL_PG_HOST}:$LOCAL_PG_PORT"
    "$pg_ctl_bin" \
      -D "$LOCAL_PG_DATA_DIR" \
      -l "$LOCAL_PG_LOG" \
      -o "-p $LOCAL_PG_PORT -h $LOCAL_PG_HOST -k '$LOCAL_PG_SOCKET_DIR'" \
      start \
      >/dev/null
  fi

  if ! "$pg_isready_bin" -h "$LOCAL_PG_HOST" -p "$LOCAL_PG_PORT" >/dev/null 2>&1; then
    log "本地 PostgreSQL 未就绪，日志见 $LOCAL_PG_LOG"
    return 1
  fi

  if [[ "$("$psql_bin" -h "$LOCAL_PG_HOST" -p "$LOCAL_PG_PORT" -U "$LOCAL_PG_USER" -d postgres -tAc "SELECT 1 FROM pg_database WHERE datname = '$LOCAL_PG_DB'")" != "1" ]]; then
    log "创建本地开发库: $LOCAL_PG_DB"
    "$createdb_bin" -h "$LOCAL_PG_HOST" -p "$LOCAL_PG_PORT" -U "$LOCAL_PG_USER" "$LOCAL_PG_DB"
  fi

  export DATABASE_URL="postgresql+asyncpg://${LOCAL_PG_USER}@${LOCAL_PG_HOST}:${LOCAL_PG_PORT}/${LOCAL_PG_DB}"
}

ensure_database() {
  if initialize_database_once; then
    log "数据库 schema 已就绪"
    return 0
  fi

  if database_config_is_default || database_url_targets_local_bootstrap_postgres; then
    if database_url_targets_local_bootstrap_postgres; then
      log "DATABASE_URL 指向仓库内本地 PostgreSQL，尝试自动拉起"
    else
      log "默认数据库配置不可用，切换到仓库内 PostgreSQL 开发库"
    fi
    bootstrap_local_postgres || return 1
    initialize_database_once || {
      log "本地 PostgreSQL 已启动，但 schema 初始化失败，日志见 $DATABASE_INIT_LOG"
      return 1
    }
    log "数据库 schema 已就绪"
    log "当前 DATABASE_URL: ${DATABASE_URL}"
    return 0
  fi

  log "数据库初始化失败，请检查配置文件中的 DATABASE_URL / DATABASE_*，日志见 $DATABASE_INIT_LOG"
  return 1
}

ensure_frontend_dist() {
  if [[ -f "$FRONTEND_DIST_DIR/index.html" ]]; then
    return 0
  fi

  if [[ "$PACKAGE_MODE" -eq 1 ]]; then
    log "发布包缺少 frontend/dist，无法启动前端"
    return 1
  fi

  require_command npm
  if [[ ! -d "$APP_DIR/frontend/node_modules" ]]; then
    log "安装前端依赖"
    if [[ -f "$APP_DIR/frontend/package-lock.json" ]]; then
      npm --prefix "$APP_DIR/frontend" ci
    else
      npm --prefix "$APP_DIR/frontend" install
    fi
  fi

  log "构建前端静态资源"
  VITE_API_BASE_URL=/api npm --prefix "$APP_DIR/frontend" run build
}

ensure_backend() {
  cleanup_pid_file_process "$BACKEND_PID_FILE"
  reclaim_managed_port "$BACKEND_PORT" "后端服务" "polymarket_trader.api.app:create_app" || return 1
  ensure_database

  log "启动后端服务"
  start_background_process \
    "'$PYTHON_BIN' -m uvicorn polymarket_trader.api.app:create_app --factory --host '$BACKEND_HOST' --port '$BACKEND_PORT' --timeout-graceful-shutdown 5" \
    "$BACKEND_PID_FILE"

  # 后端 lifespan startup 含 DB connect + 同步 reconcile_once,冷启动接近 30s 上限,
  # 偶发触发 wait_for_http 超时报"启动失败"虽然后端仍在跑——提到 60s 留充裕余量。
  wait_for_http "后端服务" "$BACKEND_HEALTH_URL" 60
  log "后端已启动: $BACKEND_HEALTH_URL"
}

ensure_frontend() {
  ensure_frontend_dist

  cleanup_pid_file_process "$FRONTEND_PID_FILE"
  reclaim_managed_port "$FRONTEND_PORT" "前端服务" "serve_frontend.py" || return 1

  log "启动前端静态服务"
  start_background_process \
    "'$PYTHON_BIN' '$APP_DIR/support/serve_frontend.py' --host '$FRONTEND_HOST' --port '$FRONTEND_PORT' --static-dir '$APP_DIR/frontend/dist' --backend-base-url '$BACKEND_BASE_URL'" \
    "$FRONTEND_PID_FILE"

  wait_for_http "前端服务" "$FRONTEND_URL" 30
  log "前端已启动: $FRONTEND_URL"
}

open_browser() {
  local url="$1"
  : > "$BROWSER_OPEN_LOG"

  if command -v xdg-open >/dev/null 2>&1; then
    if xdg-open "$url" >>"$BROWSER_OPEN_LOG" 2>&1; then
      log "已尝试通过 xdg-open 打开浏览器: $url"
      return 0
    fi
  fi

  if command -v gio >/dev/null 2>&1; then
    if gio open "$url" >>"$BROWSER_OPEN_LOG" 2>&1; then
      log "已尝试通过 gio open 打开浏览器: $url"
      return 0
    fi
  fi

  if python_run -m webbrowser "$url" >>"$BROWSER_OPEN_LOG" 2>&1; then
    log "已尝试通过 python webbrowser 打开浏览器: $url"
    return 0
  fi

  log "自动打开浏览器失败，请手动访问: $url"
  if [[ -s "$BROWSER_OPEN_LOG" ]]; then
    log "浏览器打开日志: $BROWSER_OPEN_LOG"
  fi
  return 1
}

main() {
  ensure_runtime_python
  load_env
  stop_existing_services
  ensure_backend
  ensure_frontend
  if ! open_browser "$FRONTEND_URL"; then
    :
  fi

  log "全部服务已就绪"
  log "前端地址: $FRONTEND_URL"
  log "后端文档: ${BACKEND_BASE_URL}/docs"
  log "前端静态目录: $FRONTEND_DIST_DIR"
  log "应用目录: $APP_DIR"
  log "日志目录: $RUNTIME_DIR"
}

main "$@"
