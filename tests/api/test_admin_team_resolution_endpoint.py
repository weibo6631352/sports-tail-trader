"""``GET /admin/outright/team-resolution`` route 契约。

诊断 OUTRIGHT_TEAM_NOT_RESOLVED 的入口；route 层校验 condition_id/market_slug
至少传一个，service 层返回 None → 404，否则透传 trace。
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
        self.payload: dict[str, Any] | None = None

    async def outright_team_resolution(self, **kwargs: Any) -> dict[str, Any] | None:
        self.calls["outright_team_resolution"] = kwargs
        return self.payload


@pytest.fixture()
def http_client() -> tuple[TestClient, _RecordingService]:
    service = _RecordingService()
    app = FastAPI()
    app.include_router(sports_router)
    app.dependency_overrides[get_admin_service] = lambda: service
    return TestClient(app), service


def test_team_resolution_400_when_no_identifier_supplied(
    http_client: tuple[TestClient, _RecordingService],
) -> None:
    client, _ = http_client
    response = client.get("/admin/outright/team-resolution")
    assert response.status_code == 400
    assert response.json()["detail"] == "condition_id_or_market_slug_required"


def test_team_resolution_404_when_market_missing(
    http_client: tuple[TestClient, _RecordingService],
) -> None:
    client, service = http_client
    service.payload = None
    response = client.get(
        "/admin/outright/team-resolution",
        params={"condition_id": "cond-unknown"},
    )
    assert response.status_code == 404
    assert response.json()["detail"] == "market_not_found"


def test_team_resolution_returns_trace_when_resolved(
    http_client: tuple[TestClient, _RecordingService],
) -> None:
    client, service = http_client
    service.payload = {
        "market": {
            "condition_id": "cond-celtics",
            "market_slug": "will-celtics-win-2026",
            "event_slug": "2026-nba-championship-winner",
            "market_question": "Will the Boston Celtics win the 2026 NBA championship?",
            "event_title": "2026 NBA Champion",
        },
        "snapshot_available": True,
        "snapshot_market_key": "2026-nba-championship-winner",
        "snapshot_source": "theoddsapi",
        "snapshot_observed_at": "2026-05-13T18:00:00+00:00",
        "trace": {
            "market_question": "Will the Boston Celtics win the 2026 NBA championship?",
            "event_title": "2026 NBA Champion",
            "event_slug": "2026-nba-championship-winner",
            "normalized_text": "will the boston celtics win the 2026 nba championship",
            "candidate_teams": ["Boston Celtics", "Denver Nuggets"],
            "matches": ["Boston Celtics"],
            "resolved": "Boston Celtics",
            "ambiguous": False,
        },
    }
    response = client.get(
        "/admin/outright/team-resolution",
        params={"condition_id": "cond-celtics"},
    )
    assert response.status_code == 200
    body = response.json()
    assert body["trace"]["resolved"] == "Boston Celtics"
    assert body["snapshot_available"] is True
    assert service.calls["outright_team_resolution"] == {
        "condition_id": "cond-celtics",
        "market_slug": None,
    }


def test_team_resolution_returns_missing_snapshot_reason(
    http_client: tuple[TestClient, _RecordingService],
) -> None:
    """市场存在但 snapshot 缺失 → 200 + trace=None + reason=missing_season_odds。"""

    client, service = http_client
    service.payload = {
        "market": {
            "condition_id": "cond-x",
            "market_slug": "m-x",
            "event_slug": "e-x",
            "market_question": "Q",
            "event_title": "T",
        },
        "snapshot_available": False,
        "reason": "missing_season_odds",
        "trace": None,
    }
    response = client.get(
        "/admin/outright/team-resolution",
        params={"market_slug": "m-x"},
    )
    assert response.status_code == 200
    body = response.json()
    assert body["snapshot_available"] is False
    assert body["reason"] == "missing_season_odds"
    assert body["trace"] is None


def test_team_resolution_accepts_market_slug_only(
    http_client: tuple[TestClient, _RecordingService],
) -> None:
    client, service = http_client
    service.payload = {
        "market": {},
        "snapshot_available": False,
        "reason": "missing_season_odds",
        "trace": None,
    }
    response = client.get(
        "/admin/outright/team-resolution",
        params={"market_slug": "will-celtics-win-2026"},
    )
    assert response.status_code == 200
    assert service.calls["outright_team_resolution"] == {
        "condition_id": None,
        "market_slug": "will-celtics-win-2026",
    }
