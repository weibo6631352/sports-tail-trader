from __future__ import annotations

import fcntl
import logging
import os
import sys
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Any

from fastapi import FastAPI, Request
from fastapi.middleware.cors import CORSMiddleware
from starlette.middleware.base import RequestResponseEndpoint
from starlette.responses import JSONResponse, Response

from polymarket_trader.app.admin_service import AdminService
from polymarket_trader.app.analytics_service import AnalyticsService, SessionFactoryAnalyticsDAO
from polymarket_trader.config import Settings
from polymarket_trader.api.routes import (
    allocations,
    analytics,
    audit_events,
    candidates,
    exports,
    fills,
    health,
    markets,
    operations,
    orders,
    outbox,
    parameters,
    portfolio,
    positions,
    sports,
    stream,
    trade_replays,
    trades,
)
from polymarket_trader.api.routes import runtime as runtime_route
from polymarket_trader.main import create_runtime, shutdown_runtime

logger = logging.getLogger(__name__)

# 鉴权豁免的"只读 / 公开"前缀。匹配是前缀字符串匹配，足够清晰；将来如要把
# 单个端点加入豁免，扩展这个集合即可。
_AUTH_EXEMPT_PREFIXES: tuple[str, ...] = (
    "/health",
    "/ready",
    "/openapi.json",
    "/docs",
    "/redoc",
    # SSE / 只读 stream：前端 EventSource 不支持自定义 header，浏览器侧靠 CORS + token query 防御
    "/stream/",
)


def _parse_cors_origins(raw: str) -> list[str]:
    return [item.strip() for item in raw.split(",") if item.strip()]


_SINGLETON_LOCK_HANDLE: Any = None


def _acquire_singleton_lock() -> Any | None:
    """实盘防多开:两个 backend 同时连同一账户会双下单——OS 文件锁防御。

    端口绑定本身已是天然单实例锁(uvicorn EADDRINUSE 报错),但用户可能
    用 APP_BACKEND_PORT 改端口绕开;这里加一层 fcntl 文件锁兜底,绑定
    资源粒度(账户)而非端口。锁文件 fd 一直持有到进程退出,自动释放。
    runtime 路径:dev=.dev-runtime, package=.runtime,由 APP_RUNTIME_DIR
    或当前工作目录派生——和 start_all.sh 保持一致。
    """
    runtime_dir = Path(os.environ.get("APP_RUNTIME_DIR") or ".dev-runtime")
    if not runtime_dir.exists():
        runtime_dir = Path(".runtime")
        if not runtime_dir.exists():
            runtime_dir = Path(".dev-runtime")
            runtime_dir.mkdir(parents=True, exist_ok=True)
    global _SINGLETON_LOCK_HANDLE
    if _SINGLETON_LOCK_HANDLE is not None:
        return _SINGLETON_LOCK_HANDLE  # 同进程内重复 create_app 复用锁
    lock_path = runtime_dir / "backend.lock"
    lock_fd = open(lock_path, "w")  # noqa: SIM115 - intentional process lifetime
    try:
        fcntl.flock(lock_fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except BlockingIOError:
        logger.error(
            "另一个 backend 实例已持有锁 %s——拒绝启动,防止双下单(实盘安全)",
            lock_path,
        )
        lock_fd.close()
        sys.exit(2)
    lock_fd.write(str(os.getpid()))
    lock_fd.flush()
    _SINGLETON_LOCK_HANDLE = lock_fd  # 模块级持有,直到进程退出 fd 释放自动解锁
    return lock_fd


def create_app(
    *,
    runtime: Any | None = None,
    admin_service: AdminService | None = None,
    settings: Settings | None = None,
) -> FastAPI:
    owns_runtime = runtime is None
    resolved_settings = settings or Settings()
    # 在 lifespan 之外(进程级)acquire 锁——uvicorn 启动期 import 阶段就拦截,
    # 第二个实例直接 sys.exit 不会走到 lifespan。锁 fd 由模块全局持有到进程退出。
    if owns_runtime:
        _acquire_singleton_lock()

    @asynccontextmanager
    async def lifespan(app: FastAPI):
        bound_runtime = runtime
        if bound_runtime is None:
            bound_runtime = await create_runtime()
        bound_service = admin_service or getattr(bound_runtime, "admin_service", None) or AdminService()
        if getattr(bound_service, "runtime", None) is None:
            bound_service = bound_service.bind_runtime(bound_runtime)
        app.state.runtime = bound_runtime
        app.state.admin_service = bound_service
        app.state.analytics_service = AnalyticsService(
            dao=SessionFactoryAnalyticsDAO(bound_runtime.db_session_factory),
        )
        app.state.get_runtime = lambda: app.state.runtime
        app.state.get_admin_service = lambda: app.state.admin_service
        if getattr(bound_runtime, "admin_service", None) is None:
            bound_runtime.admin_service = bound_service
        try:
            yield
        finally:
            if owns_runtime:
                await shutdown_runtime(bound_runtime)

    docs_url = "/docs" if resolved_settings.expose_openapi_docs else None
    redoc_url = "/redoc" if resolved_settings.expose_openapi_docs else None
    openapi_url = "/openapi.json" if resolved_settings.expose_openapi_docs else None
    app = FastAPI(
        title="Polymarket Trader Admin API",
        lifespan=lifespan,
        docs_url=docs_url,
        redoc_url=redoc_url,
        openapi_url=openapi_url,
    )

    # === Admin token 鉴权 middleware ===
    # ADMIN_API_TOKEN 未配置 → 路由裸跑（仅本机开发可接受），启动期输出 warning；
    # 配置了 → 所有非豁免路径必须带 X-Admin-Token: <匹配值>，否则 401。
    expected_token = (
        resolved_settings.admin_api_token.get_secret_value()
        if resolved_settings.admin_api_token is not None
        else None
    )
    if expected_token is None:
        logger.warning(
            "admin_api_token not set — admin API endpoints accept anonymous requests. "
            "Set ADMIN_API_TOKEN in .env / environment for any non-local deployment."
        )

    # === HTTP 性能监控 middleware（统一记录所有 endpoint latency / error）===
    from polymarket_trader.runtime.system_perf_monitor import SystemPerfMonitor
    _perf_monitor = SystemPerfMonitor.get()

    @app.middleware("http")
    async def _perf_middleware(request: Request, call_next: RequestResponseEndpoint) -> Response:
        """区分 程序处理时间（handler_ms）vs 响应大小（流量）。

        - handler_ms: middleware 测的 = handler 执行 + 响应序列化（不含网络传输）
        - response_bytes: 响应体大小 → 客户端总等待 ≈ handler_ms + transmit
        """
        import time as _time
        start = _time.time()
        error = False
        response_bytes = 0
        response = None
        try:
            _perf_monitor.http_request_enter()
        except Exception: pass
        try:
            response = await call_next(request)
            if response.status_code >= 500:
                error = True
            # 拿 Content-Length（不强制 buffer 整个 body）
            cl = response.headers.get("content-length")
            if cl and cl.isdigit():
                response_bytes = int(cl)
            return response
        except Exception:
            error = True
            raise
        finally:
            try:
                _perf_monitor.record_http(
                    endpoint=f"{request.method} {request.url.path}",
                    handler_ms=(_time.time() - start) * 1000,
                    response_bytes=response_bytes,
                    error=error,
                )
                _perf_monitor.http_request_exit()
            except Exception:
                pass

    @app.middleware("http")
    async def _admin_token_middleware(request: Request, call_next: RequestResponseEndpoint) -> Response:
        if expected_token is None:
            return await call_next(request)
        path = request.url.path
        if any(path.startswith(prefix) for prefix in _AUTH_EXEMPT_PREFIXES):
            return await call_next(request)
        # OPTIONS 是 CORS 预检——由 CORSMiddleware 处理，跳过 token 校验
        if request.method == "OPTIONS":
            return await call_next(request)
        provided = request.headers.get("x-admin-token")
        if provided != expected_token:
            return JSONResponse(status_code=401, content={"detail": "admin_token_required"})
        return await call_next(request)

    app.add_middleware(
        CORSMiddleware,
        allow_origins=_parse_cors_origins(resolved_settings.cors_allowed_origins),
        allow_credentials=False,
        allow_methods=["*"],
        allow_headers=["*"],
    )
    app.include_router(health.router)
    app.include_router(runtime_route.router)
    app.include_router(audit_events.router)
    app.include_router(candidates.router)
    app.include_router(allocations.router)
    app.include_router(markets.router)
    app.include_router(orders.router)
    app.include_router(fills.router)
    app.include_router(positions.router)
    app.include_router(trade_replays.router)
    app.include_router(trades.router)
    app.include_router(sports.router)
    app.include_router(parameters.router)
    app.include_router(portfolio.router)
    app.include_router(outbox.router)
    app.include_router(operations.router)
    app.include_router(analytics.router)
    app.include_router(exports.router)
    app.include_router(stream.router)
    return app
