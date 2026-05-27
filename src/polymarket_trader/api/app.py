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
from starlette.responses import Response

from polymarket_trader.app.operator_service import OperatorService
from polymarket_trader.config import Settings
from polymarket_trader.api.routes import (
    allocations,
    analytics,
    audit_events,
    candidates,
    decision_context,
    exports,
    fills,
    health,
    markets,
    operations,
    orders,
    outbox,
    portfolio,
    positions,
    signals,
    sports,
    stream,
    trade_replays,
    trades,
    ws_operator,
)
from polymarket_trader.api.routes import runtime as runtime_route
from polymarket_trader.main import create_runtime, shutdown_runtime

logger = logging.getLogger(__name__)


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
    operator_service: OperatorService | None = None,
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
        bound_service = operator_service or getattr(bound_runtime, "operator_service", None) or OperatorService()
        if getattr(bound_service, "runtime", None) is None:
            bound_service = bound_service.bind_runtime(bound_runtime)
        app.state.runtime = bound_runtime
        app.state.operator_service = bound_service
        app.state.get_runtime = lambda: app.state.runtime
        app.state.get_operator_service = lambda: app.state.operator_service
        if getattr(bound_runtime, "operator_service", None) is None:
            bound_runtime.operator_service = bound_service
        try:
            yield
        finally:
            if owns_runtime:
                await shutdown_runtime(bound_runtime)

    docs_url = "/docs" if resolved_settings.expose_openapi_docs else None
    redoc_url = "/redoc" if resolved_settings.expose_openapi_docs else None
    openapi_url = "/openapi.json" if resolved_settings.expose_openapi_docs else None
    app = FastAPI(
        title="Polymarket Trader Operator API",
        lifespan=lifespan,
        docs_url=docs_url,
        redoc_url=redoc_url,
        openapi_url=openapi_url,
    )

    # === Admin token 鉴权 middleware ===
    # 实现在 api/middleware/auth.py；豁免前缀也维护在那里
    from polymarket_trader.api.middleware.auth import install_admin_token_middleware
    expected_token = (
        resolved_settings.admin_api_token.get_secret_value()
        if resolved_settings.admin_api_token is not None
        else None
    )

    # === HTTP 性能监控 middleware（统一记录所有 endpoint latency / error）===
    # 双写：SystemPerfMonitor（operator 内部分析）+ MetricsRegistry（§11.2 标准 metric output）
    from polymarket_trader.runtime.system_perf_monitor import SystemPerfMonitor
    _perf_monitor = SystemPerfMonitor.get()

    # === trace_id middleware（CLAUDE.md §17 复盘可追溯）===
    # 每个 operator 请求生成 trace_id（或继承 client 传入的 X-Trace-Id），response
    # header 回传——运维 / agent 看到响应能直接 grep audit_events 找完整链路。
    from polymarket_trader.observability.trace import bind_trace_id, ensure_trace_id

    @app.middleware("http")
    async def _trace_id_middleware(request: Request, call_next: RequestResponseEndpoint) -> Response:
        incoming = request.headers.get("x-trace-id")
        trace_id = bind_trace_id(incoming) if incoming else ensure_trace_id()
        response = await call_next(request)
        response.headers["X-Trace-Id"] = trace_id
        return response

    @app.middleware("http")
    async def _perf_middleware(request: Request, call_next: RequestResponseEndpoint) -> Response:
        """区分 程序处理时间（handler_ms）vs 响应大小（流量）。

        - handler_ms: middleware 测的 = handler 执行 + 响应序列化（不含网络传输）
        - response_bytes: 响应体大小 → 客户端总等待 ≈ handler_ms + transmit
        - metrics 写入用 route template（含 {cid} 占位）避免高基数 label 爆炸（§11.5）
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
            elapsed_ms = (_time.time() - start) * 1000
            try:
                _perf_monitor.record_http(
                    endpoint=f"{request.method} {request.url.path}",
                    handler_ms=elapsed_ms,
                    response_bytes=response_bytes,
                    error=error,
                )
                _perf_monitor.http_request_exit()
            except Exception:
                pass
            # MetricsRegistry 写入（§11.2 api_endpoint_*）
            try:
                runtime_obj = getattr(app.state, "runtime", None)
                if runtime_obj is not None and runtime_obj.metrics is not None:
                    route = request.scope.get("route")
                    route_path = getattr(route, "path", request.url.path) if route else request.url.path
                    status_code = response.status_code if response is not None else 500
                    labels = {"route": route_path, "method": request.method, "status": str(status_code)}
                    runtime_obj.metrics.observe_latency("api_endpoint_latency_ms", elapsed_ms, labels=labels)
                    # payload 体积 histogram 用独立 byte buckets, 供 HealthReporter.endpoints()
                    # 检测 §17.9 50KB 上限超标.
                    runtime_obj.metrics.observe_size(
                        "api_endpoint_response_bytes", response_bytes, labels=labels,
                    )
                    runtime_obj.metrics.inc_counter("api_endpoint_requests_total", labels=labels)
                    if error:
                        runtime_obj.metrics.inc_counter(
                            "api_endpoint_errors_total",
                            labels={"route": route_path, "method": request.method},
                        )
            except Exception:
                pass

    install_admin_token_middleware(app, expected_token=expected_token)

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
    app.include_router(decision_context.router)
    app.include_router(candidates.router)
    app.include_router(allocations.router)
    app.include_router(markets.router)
    app.include_router(orders.router)
    app.include_router(fills.router)
    app.include_router(positions.router)
    app.include_router(trade_replays.router)
    app.include_router(trades.router)
    app.include_router(signals.router)
    app.include_router(sports.router)
    app.include_router(portfolio.router)
    app.include_router(outbox.router)
    app.include_router(operations.router)
    app.include_router(analytics.router)
    app.include_router(exports.router)
    app.include_router(stream.router)
    app.include_router(ws_operator.router)
    return app
