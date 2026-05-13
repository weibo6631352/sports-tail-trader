"""``GET /admin/series/state`` route 契约 + service 透传。

Worktree 5 引入的诊断 endpoint，让运维能从 admin 端确认 series_state_worker 是否
在持续刷新比分。route 层的职责限于参数透传、分页校验、依赖注入；service 层
``list_series_state_snapshots`` 已在自己的单测覆盖。
"""

from __future__ import annotations

from typing import Any

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from polymarket_trader.api.deps import get_admin_service
from polymarket_trader.api.routes.sports import router as sports_router


class _RecordingService:
    def __init__(self) -> None:
        self.calls: dict[str, Any] = {}
        self.snapshots: list[dict[str, Any]] = []

    async def list_series_state_snapshots(self, **kwargs: Any) -> dict[str, Any]:
        self.calls["list_series_state_snapshots"] = kwargs
        return {
            "items": self.snapshots,
            "total_records": len(self.snapshots),
            "limit": kwargs.get("limit", 0),
            "offset": kwargs.get("offset", 0),
            "total": len(self.snapshots),
        }


@pytest.fixture()
def http_client() -> tuple[TestClient, _RecordingService]:
    service = _RecordingService()
    app = FastAPI()
    app.include_router(sports_router)
    app.dependency_overrides[get_admin_service] = lambda: service
    return TestClient(app), service


def test_admin_series_state_returns_empty_array_when_store_empty(
    http_client: tuple[TestClient, _RecordingService],
) -> None:
    client, service = http_client
    response = client.get("/admin/series/state")
    assert response.status_code == 200
    body = response.json()
    assert body["items"] == []
    assert body["total_records"] == 0
    # 默认 limit/offset 仍透传给 service，避免 service 端拒绝 None。
    kwargs = service.calls["list_series_state_snapshots"]
    assert kwargs["limit"] == 200
    assert kwargs["offset"] == 0


def test_admin_series_state_returns_snapshots_when_present(
    http_client: tuple[TestClient, _RecordingService],
) -> None:
    client, service = http_client
    service.snapshots = [
        {
            "condition_id": "cond-A",
            "market_slug": "celtics-vs-knicks",
            "event_slug": "celtics-vs-knicks-2026",
            "team_a": "Boston Celtics",
            "team_b": "New York Knicks",
            "wins_a": 2,
            "wins_b": 1,
            "best_of": 7,
            "observed_at": "2026-05-13T18:00:00+00:00",
            "age_seconds": 12.0,
            "source": "series_state:espn",
        },
        {
            "condition_id": "cond-B",
            "market_slug": "ducks-vs-oilers",
            "event_slug": "ducks-vs-oilers-2026",
            "team_a": "Anaheim Ducks",
            "team_b": "Edmonton Oilers",
            "wins_a": 1,
            "wins_b": 0,
            "best_of": 7,
            "observed_at": "2026-05-13T17:30:00+00:00",
            "age_seconds": 1812.0,
            "source": "series_state:espn",
        },
    ]
    response = client.get("/admin/series/state")
    assert response.status_code == 200
    body = response.json()
    assert body["total_records"] == 2
    assert {item["condition_id"] for item in body["items"]} == {"cond-A", "cond-B"}


def test_admin_series_state_respects_pagination(
    http_client: tuple[TestClient, _RecordingService],
) -> None:
    client, service = http_client
    response = client.get("/admin/series/state", params={"limit": 50, "offset": 25})
    assert response.status_code == 200
    kwargs = service.calls["list_series_state_snapshots"]
    assert kwargs["limit"] == 50
    assert kwargs["offset"] == 25


def test_admin_series_state_rejects_invalid_limit(
    http_client: tuple[TestClient, _RecordingService],
) -> None:
    client, _ = http_client
    # limit < 1 → 422
    response = client.get("/admin/series/state", params={"limit": 0})
    assert response.status_code == 422
