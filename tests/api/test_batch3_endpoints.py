"""Batch 3 路由透传 + 服务调用契约。

覆盖：
- ``GET /sports/live-events`` → ``list_sports_live_events_history``
- ``GET /allocations/decisions`` → ``list_allocation_decisions``
- ``GET /analytics/risk-rejections`` / ``aggregate`` → 各对应服务方法
- ``GET /analytics/calibration`` → ``calibration_snapshot``，``bucket_size`` 转 Decimal
- ``GET /markets/settlements`` → ``list_market_settlements``
- ``POST /markets/settle`` → ``record_market_settlement``，必填字段缺失返回 422
"""

from __future__ import annotations

from decimal import Decimal
from typing import Any

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from polymarket_trader.api.deps import get_admin_service
from polymarket_trader.api.routes.allocations import router as allocations_router
from polymarket_trader.api.routes.analytics import router as analytics_router
from polymarket_trader.api.routes.markets import router as markets_router
from polymarket_trader.api.routes.sports import router as sports_router


class _RecordingService:
    def __init__(self) -> None:
        self.calls: dict[str, Any] = {}

    async def list_sports_live_events_history(self, **kwargs: Any) -> dict[str, Any]:
        self.calls["list_sports_live_events_history"] = kwargs
        return {"items": [], "total": 0, "limit": kwargs.get("limit", 200), "offset": 0}

    async def list_allocation_decisions(self, **kwargs: Any) -> dict[str, Any]:
        self.calls["list_allocation_decisions"] = kwargs
        return {"items": [], "total": 0, "limit": kwargs.get("limit", 200), "offset": 0}

    async def list_risk_rejections(self, **kwargs: Any) -> dict[str, Any]:
        self.calls["list_risk_rejections"] = kwargs
        return {"items": [], "total": 0, "limit": kwargs.get("limit", 200), "offset": 0}

    async def aggregate_risk_rejections(self, **kwargs: Any) -> dict[str, Any]:
        self.calls["aggregate_risk_rejections"] = kwargs
        return {"total_rejections": 0, "by_check_name": [], "by_failed_field": []}

    async def calibration_snapshot(self, **kwargs: Any) -> dict[str, Any]:
        self.calls["calibration_snapshot"] = kwargs
        return {
            "bucket_size": str(kwargs["bucket_size"]),
            "buckets": [],
            "brier_score": None,
            "log_loss": None,
            "total_samples": 0,
            "with_outcome_count": 0,
        }

    async def list_market_settlements(self, **kwargs: Any) -> dict[str, Any]:
        self.calls["list_market_settlements"] = kwargs
        return {"items": [], "total": 0, "limit": kwargs.get("limit", 200), "offset": 0}

    async def record_market_settlement(self, **kwargs: Any) -> dict[str, Any]:
        self.calls["record_market_settlement"] = kwargs
        return {"status": "ok", **kwargs}


@pytest.fixture()
def http_client() -> tuple[TestClient, _RecordingService]:
    service = _RecordingService()
    app = FastAPI()
    app.include_router(sports_router)
    app.include_router(allocations_router)
    app.include_router(analytics_router)
    app.include_router(markets_router)
    app.dependency_overrides[get_admin_service] = lambda: service
    return TestClient(app), service


# ----------------------------- /sports/live-events -----------------------------


def test_sports_live_events_route(http_client: tuple[TestClient, _RecordingService]) -> None:
    client, service = http_client
    response = client.get(
        "/sports/live-events",
        params={"condition_id": "cond-X", "since": 1, "until": 2, "limit": 50},
    )
    assert response.status_code == 200
    kwargs = service.calls["list_sports_live_events_history"]
    assert kwargs["condition_id"] == "cond-X"
    assert kwargs["limit"] == 50
    tr = kwargs["time_range"]
    assert tr is not None and (tr.since_ms, tr.until_ms) == (1, 2)


# ------------------------- /allocations/decisions ------------------------------


def test_allocation_decisions_route(http_client: tuple[TestClient, _RecordingService]) -> None:
    client, service = http_client
    response = client.get(
        "/allocations/decisions",
        params={"condition_id": "cond-A", "since": 10, "limit": 30},
    )
    assert response.status_code == 200
    kwargs = service.calls["list_allocation_decisions"]
    assert kwargs["condition_id"] == "cond-A"
    assert kwargs["limit"] == 30
    tr = kwargs["time_range"]
    assert tr is not None and tr.since_ms == 10


# ----------------------- /analytics/risk-rejections ----------------------------


def test_risk_rejections_list_route(http_client: tuple[TestClient, _RecordingService]) -> None:
    client, service = http_client
    response = client.get(
        "/analytics/risk-rejections",
        params={"condition_id": "cond-Y", "limit": 100},
    )
    assert response.status_code == 200
    assert service.calls["list_risk_rejections"]["condition_id"] == "cond-Y"
    assert service.calls["list_risk_rejections"]["limit"] == 100


def test_risk_rejections_aggregate_route(http_client: tuple[TestClient, _RecordingService]) -> None:
    client, service = http_client
    response = client.get(
        "/analytics/risk-rejections/aggregate",
        params={"sample_limit": 500, "since": 100, "until": 200},
    )
    assert response.status_code == 200
    kwargs = service.calls["aggregate_risk_rejections"]
    assert kwargs["sample_limit"] == 500
    tr = kwargs["time_range"]
    assert tr is not None and (tr.since_ms, tr.until_ms) == (100, 200)


# ----------------------------- /analytics/calibration --------------------------


def test_calibration_route_converts_bucket_size_to_decimal(
    http_client: tuple[TestClient, _RecordingService],
) -> None:
    client, service = http_client
    response = client.get("/analytics/calibration", params={"bucket_size": 0.1, "sample_limit": 100})
    assert response.status_code == 200
    kwargs = service.calls["calibration_snapshot"]
    assert kwargs["bucket_size"] == Decimal("0.1")
    assert kwargs["sample_limit"] == 100


# ------------------------------- /markets/settlements -------------------------


def test_settlements_list_route(http_client: tuple[TestClient, _RecordingService]) -> None:
    client, service = http_client
    response = client.get("/markets/settlements", params={"condition_id": "cond-Z"})
    assert response.status_code == 200
    assert service.calls["list_market_settlements"]["condition_id"] == "cond-Z"


def test_settlement_post_records_event(http_client: tuple[TestClient, _RecordingService]) -> None:
    client, service = http_client
    response = client.post(
        "/markets/settle",
        json={
            "condition_id": "cond-W",
            "winning_token_id": "tok-yes",
            "winning_outcome": "Yes",
            "source": "manual",
            "operator": "agent",
        },
    )
    assert response.status_code == 200
    kwargs = service.calls["record_market_settlement"]
    assert kwargs["condition_id"] == "cond-W"
    assert kwargs["winning_token_id"] == "tok-yes"


def test_settlement_post_requires_winning_token(http_client: tuple[TestClient, _RecordingService]) -> None:
    client, _ = http_client
    response = client.post("/markets/settle", json={"condition_id": "cond-W"})
    assert response.status_code == 422
