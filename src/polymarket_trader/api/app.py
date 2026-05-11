from __future__ import annotations

from contextlib import asynccontextmanager
from typing import Any

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware

from polymarket_trader.app.admin_service import AdminService
from polymarket_trader.app.analytics_service import AnalyticsService, SessionFactoryAnalyticsDAO
from polymarket_trader.api.routes import (
    allocations,
    analytics,
    audit_events,
    candidates,
    fills,
    health,
    markets,
    operations,
    orders,
    outbox,
    portfolio,
    positions,
    trade_replays,
)
from polymarket_trader.api.routes import runtime as runtime_route
from polymarket_trader.main import create_runtime, shutdown_runtime


def create_app(*, runtime: Any | None = None, admin_service: AdminService | None = None) -> FastAPI:
    owns_runtime = runtime is None

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

    app = FastAPI(title="Polymarket Trader Admin API", lifespan=lifespan)
    app.add_middleware(
        CORSMiddleware,
        allow_origins=[
            "http://127.0.0.1:5173",
            "http://127.0.0.1:5174",
            "http://localhost:5173",
            "http://localhost:5174",
        ],
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
    app.include_router(portfolio.router)
    app.include_router(outbox.router)
    app.include_router(operations.router)
    app.include_router(analytics.router)
    return app
