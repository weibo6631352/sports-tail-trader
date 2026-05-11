"""Verify ``since`` / ``until`` query plumbing on the three list endpoints.

Each route must:
- forward ``since`` / ``until`` epoch_ms as a ``TimeRange`` value object to the
  underlying AdminService method (no per-route conversion drift);
- reject ``since > until`` with HTTP 400 ``since_after_until``;
- pass ``time_range=None`` when neither bound is supplied (preserving today's
  default behavior of unbounded queries).
"""

from __future__ import annotations

from typing import Any

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from polymarket_trader.api.deps import get_admin_service
from polymarket_trader.api.routes.audit_events import router as audit_events_router
from polymarket_trader.api.routes.fills import router as fills_router
from polymarket_trader.api.routes.orders import router as orders_router
from polymarket_trader.domain.time_filters import TimeRange


class _RecordingAdminService:
    """Captures kwargs passed to list_* without touching the real runtime."""

    def __init__(self) -> None:
        self.last_kwargs: dict[str, Any] | None = None

    async def list_orders(self, **kwargs: Any) -> dict[str, Any]:
        self.last_kwargs = kwargs
        return {"items": [], "total": 0, "limit": kwargs.get("limit", 100), "offset": kwargs.get("offset", 0)}

    async def list_fills(self, **kwargs: Any) -> dict[str, Any]:
        self.last_kwargs = kwargs
        return {"items": [], "total": 0, "limit": kwargs.get("limit", 100), "offset": kwargs.get("offset", 0)}

    async def list_audit_events(self, **kwargs: Any) -> dict[str, Any]:
        self.last_kwargs = kwargs
        return {"items": [], "total": 0, "limit": kwargs.get("limit", 100), "offset": kwargs.get("offset", 0)}


@pytest.fixture()
def app_with_admin() -> tuple[FastAPI, _RecordingAdminService]:
    service = _RecordingAdminService()
    app = FastAPI()
    app.include_router(orders_router)
    app.include_router(fills_router)
    app.include_router(audit_events_router)
    app.dependency_overrides[get_admin_service] = lambda: service
    return app, service


@pytest.mark.parametrize("path", ["/orders", "/fills", "/audit-events"])
def test_no_since_until_yields_none_time_range(
    app_with_admin: tuple[FastAPI, _RecordingAdminService],
    path: str,
) -> None:
    app, service = app_with_admin
    with TestClient(app) as client:
        response = client.get(path)
    assert response.status_code == 200
    assert service.last_kwargs is not None
    assert service.last_kwargs.get("time_range") is None


@pytest.mark.parametrize("path", ["/orders", "/fills", "/audit-events"])
def test_since_and_until_build_time_range(
    app_with_admin: tuple[FastAPI, _RecordingAdminService],
    path: str,
) -> None:
    app, service = app_with_admin
    with TestClient(app) as client:
        response = client.get(path, params={"since": 1_000, "until": 2_000})
    assert response.status_code == 200
    assert service.last_kwargs is not None
    time_range = service.last_kwargs.get("time_range")
    assert isinstance(time_range, TimeRange)
    assert time_range.since_ms == 1_000
    assert time_range.until_ms == 2_000


@pytest.mark.parametrize("path", ["/orders", "/fills", "/audit-events"])
def test_only_since_builds_half_open_range(
    app_with_admin: tuple[FastAPI, _RecordingAdminService],
    path: str,
) -> None:
    app, service = app_with_admin
    with TestClient(app) as client:
        response = client.get(path, params={"since": 5_000})
    assert response.status_code == 200
    time_range = (service.last_kwargs or {}).get("time_range")
    assert isinstance(time_range, TimeRange)
    assert time_range.since_ms == 5_000 and time_range.until_ms is None


@pytest.mark.parametrize("path", ["/orders", "/fills", "/audit-events"])
def test_since_after_until_returns_400(
    app_with_admin: tuple[FastAPI, _RecordingAdminService],
    path: str,
) -> None:
    app, service = app_with_admin
    with TestClient(app) as client:
        response = client.get(path, params={"since": 2_000, "until": 1_000})
    assert response.status_code == 400
    assert response.json() == {"detail": "since_after_until"}
    # service must not be called once validation fails.
    assert service.last_kwargs is None


@pytest.mark.parametrize("path", ["/orders", "/fills", "/audit-events"])
def test_negative_since_rejected_by_fastapi_validation(
    app_with_admin: tuple[FastAPI, _RecordingAdminService],
    path: str,
) -> None:
    app, _service = app_with_admin
    with TestClient(app) as client:
        response = client.get(path, params={"since": -1})
    # FastAPI ge=0 constraint rejects with 422 before our handler runs.
    assert response.status_code == 422
