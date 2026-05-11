#!/usr/bin/env bash
if [ -z "${BASH_VERSION:-}" ]; then
  exec bash "$0" "$@"
fi

set -euo pipefail

ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
FRONTEND_DIR="$ROOT_DIR/frontend"
DIST_DIR="$FRONTEND_DIR/dist"
PACKAGE_ROOT="$ROOT_DIR/.dist-packages"
BUNDLE_NAME="fdv-runtime"
BUNDLE_DIR="$PACKAGE_ROOT/$BUNDLE_NAME"
WHEEL_DIR="$BUNDLE_DIR/wheels"
CONFIG_DIR="$BUNDLE_DIR/config"
SUPPORT_DIR="$BUNDLE_DIR/support"
ARCHIVE_NAME="${BUNDLE_NAME}.tar.gz"
CREATE_ARCHIVE=0

log() {
  printf '[build_dist] %s\n' "$*"
}

require_command() {
  local command_name="$1"
  if ! command -v "$command_name" >/dev/null 2>&1; then
    printf '[build_dist] 缺少命令: %s\n' "$command_name" >&2
    exit 1
  fi
}

show_help() {
  cat <<'EOF'
用法:
  ./build_dist.sh [--archive]

说明:
  1. 执行前端 typecheck、lint、build。
  2. 构建后端 wheelhouse，供发布包离线安装运行环境。
  3. 生成可运行发布目录 `.dist-packages/fdv-runtime/`：
     - `start_all.sh`
     - `stop_all.sh`
     - `frontend/dist`
     - `support/serve_frontend.py`
     - `config/.env` / `.env.example` / `.env.full.example`
     - `wheels/`
  4. 传入 `--archive` 后，额外生成 `.dist-packages/fdv-runtime.tar.gz`。
EOF
}

ensure_frontend_dependencies() {
  if [[ -d "$FRONTEND_DIR/node_modules" ]]; then
    return 0
  fi

  log "安装前端依赖"
  if [[ -f "$FRONTEND_DIR/package-lock.json" ]]; then
    npm --prefix "$FRONTEND_DIR" ci
    return 0
  fi
  npm --prefix "$FRONTEND_DIR" install
}

build_frontend_dist() {
  log "执行前端类型检查"
  npm --prefix "$FRONTEND_DIR" run typecheck

  log "执行前端静态检查"
  npm --prefix "$FRONTEND_DIR" run lint

  log "构建前端 dist"
  rm -rf "$DIST_DIR"
  VITE_API_BASE_URL=/api npm --prefix "$FRONTEND_DIR" run build

  if [[ ! -d "$DIST_DIR" ]]; then
    log "构建结束后未找到 $DIST_DIR"
    exit 1
  fi

  log "dist 已生成: $DIST_DIR"
}

build_backend_wheels() {
  log "构建后端 wheelhouse"
  rm -rf "$WHEEL_DIR"
  mkdir -p "$WHEEL_DIR"
  python3 -m pip wheel --wheel-dir "$WHEEL_DIR" "$ROOT_DIR"

  if ! compgen -G "$WHEEL_DIR/*.whl" >/dev/null; then
    log "wheelhouse 为空: $WHEEL_DIR"
    exit 1
  fi

  log "wheelhouse 已生成: $WHEEL_DIR"
}

write_bundle_readme() {
  cat > "$BUNDLE_DIR/README.txt" <<'EOF'
FDV Runtime Bundle
==================

使用方式:
1. 按需编辑 config/.env
2. 执行 ./start_all.sh
3. 停止服务时执行 ./stop_all.sh

目录说明:
- config/.env: 默认安全配置，可直接改
- config/.env.example: 最小配置模板
- config/.env.full.example: 完整配置模板
- frontend/dist: 已构建前端静态资源
- support/serve_frontend.py: 前端静态服务 + /api 反向代理脚本，被 start_all.sh 调用
- wheels: 后端本地安装包
- .runtime: 首次启动后生成的运行环境、日志和 PID 文件
EOF
}

write_build_info() {
  local python_version
  local node_version

  python_version="$(python3 --version 2>/dev/null || true)"
  node_version="$(node --version 2>/dev/null || true)"

  cat > "$BUNDLE_DIR/build-info.txt" <<EOF
build_time=$(date -Iseconds)
python_version=${python_version}
node_version=${node_version}
bundle_name=${BUNDLE_NAME}
EOF
}

assemble_bundle() {
  log "组装发布目录"
  rm -rf "$BUNDLE_DIR"
  mkdir -p "$BUNDLE_DIR" "$CONFIG_DIR" "$SUPPORT_DIR" "$BUNDLE_DIR/frontend"

  cp -R "$DIST_DIR" "$BUNDLE_DIR/frontend/dist"
  install -m 755 "$ROOT_DIR/start_all.sh" "$BUNDLE_DIR/start_all.sh"
  install -m 755 "$ROOT_DIR/stop_all.sh" "$BUNDLE_DIR/stop_all.sh"
  install -m 644 "$ROOT_DIR/.env.example" "$CONFIG_DIR/.env"
  install -m 644 "$ROOT_DIR/.env.example" "$CONFIG_DIR/.env.example"
  install -m 644 "$ROOT_DIR/.env.full.example" "$CONFIG_DIR/.env.full.example"
  install -m 644 "$ROOT_DIR/support/serve_frontend.py" "$SUPPORT_DIR/serve_frontend.py"
  write_bundle_readme
  write_build_info

  log "发布目录已生成: $BUNDLE_DIR"
}

create_archive() {
  require_command tar
  mkdir -p "$PACKAGE_ROOT"
  rm -f "$PACKAGE_ROOT/$ARCHIVE_NAME"
  tar -C "$PACKAGE_ROOT" -czf "$PACKAGE_ROOT/$ARCHIVE_NAME" "$BUNDLE_NAME"
  log "归档已生成: $PACKAGE_ROOT/$ARCHIVE_NAME"
}

parse_args() {
  while [[ $# -gt 0 ]]; do
    case "$1" in
      --archive)
        CREATE_ARCHIVE=1
        ;;
      -h|--help)
        show_help
        exit 0
        ;;
      *)
        printf '[build_dist] 未知参数: %s\n' "$1" >&2
        show_help >&2
        exit 1
        ;;
    esac
    shift
  done
}

main() {
  parse_args "$@"
  require_command python3
  require_command npm
  require_command node

  ensure_frontend_dependencies
  build_frontend_dist
  assemble_bundle
  build_backend_wheels

  if [[ "$CREATE_ARCHIVE" -eq 1 ]]; then
    create_archive
  fi

  log "构建完成"
  log "运行入口: $BUNDLE_DIR/start_all.sh"
  log "停止入口: $BUNDLE_DIR/stop_all.sh"
  log "配置目录: $CONFIG_DIR"
}

main "$@"
