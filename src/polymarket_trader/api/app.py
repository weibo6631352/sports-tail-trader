from __future__ import annotations

import logging
from contextlib import asynccontextmanager
from typing import Any

from fastapi import FastAPI, Request
from fastapi.middleware.cors import CORSMiddleware
from starlette.responses import JSONResponse

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


def create_app(
    *,
    runtime: Any | None = None,
    admin_service: AdminService | None = None,
    settings: Settings | None = None,
) -> FastAPI:
    owns_runtime = runtime is None
    resolved_settings = settings or Settings()

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

    @app.middleware("http")
    async def _admin_token_middleware(request: Request, call_next):  # type: ignore[no-untyped-def]
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
