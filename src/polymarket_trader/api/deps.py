from __future__ import annotations

from typing import Any

from fastapi import HTTPException, Request

from polymarket_trader.app.admin_service import AdminService


def get_runtime(request: Request) -> Any:
    provider = getattr(request.app.state, "get_runtime", None)
    if callable(provider):
        runtime = provider()
        if runtime is not None:
            return runtime
    runtime = getattr(request.app.state, "runtime", None)
    if runtime is None:
        raise HTTPException(status_code=503, detail="runtime_unavailable")
    return runtime


def get_admin_service(request: Request) -> AdminService:
    provider = getattr(request.app.state, "get_admin_service", None)
    if callable(provider):
        service = provider()
        if service is not None:
            return service
    service = getattr(request.app.state, "admin_service", None)
    if service is None:
        raise HTTPException(status_code=503, detail="admin_service_unavailable")
    return service
