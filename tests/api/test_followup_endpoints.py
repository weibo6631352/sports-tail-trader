"""收尾批次路由透传 + 服务调用契约。

覆盖：
- ``GET /markets/{condition_id}/settlement`` 404 / 200
- ``GET /analytics/missed-opportunities`` 参数透传 + per_decision_usdc 转 Decimal
- ``GET /portfolio/risk-metrics`` 503 / 200
- ``GET /audit-events/operators`` 参数透传
"""

from __future__ import annotations

from decimal import Decimal
from typing import Any

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from polymarket_trader.api.deps import get_admin_service
from polymarket_trader.api.routes.analytics import router as analytics_router
from polymarket_trader.api.routes.audit_events import router as audit_router
from polymarket_trader.api.routes.markets import router as markets_router
from polymarket_trader.api.routes.operations import router as operations_router
from polymarket_trader.api.routes.portfolio import router as portfolio_router


class _RecordingService:
    def __init__(self) -> None:
        self.calls: dict[str, Any] = {}
        self.settlement_payload: dict[str, Any] | None = None
        self.risk_metrics_raises: bool = False

    async def get_market_settlement(self, **kwargs: Any) -> dict[str, Any] | None:
        self.calls["get_market_settlement"] = kwargs
        return self.settlement_payload

    async def missed_opportunities_snapshot(self, **kwargs: Any) -> dict[str, Any]:
        self.calls["missed_opportunities_snapshot"] = kwargs
        return {"items": [], "by_reason": [], "totals": {}, "per_decision_usdc": "10"}

    async def portfolio_risk_metrics(self, **kwargs: Any) -> dict[str, Any]:
        self.calls["portfolio_risk_metrics"] = kwargs
        if self.risk_metrics_raises:
            raise RuntimeError("db_session_factory unavailable")
        return {"window_ms": kwargs["window_ms"], "interval_ms": kwargs["interval_ms"], "metrics": {}}

    async def aggregate_operator_interventions(self, **kwargs: Any) -> dict[str, Any]:
        self.calls["aggregate_operator_interventions"] = kwargs
        return {
            "operator": kwargs.get("operator"),
            "total_events": 0,
            "by_operator": [],
            "by_event_title": [],
            "events": [],
        }

    async def run_parameter_sweep(self, **kwargs: Any) -> dict[str, Any]:
        self.calls["run_parameter_sweep"] = kwargs
        # 路由对 422 的契约——service 在校验失败时抛 ValueError
        candidates = kwargs.get("candidates") or {}
        if "bogus_param" in candidates:
            raise ValueError("unsupported sweep parameter: bogus_param")
        return {
            "candidate_count": 1,
            "decision_sample_count": 0,
            "scorable_decision_count": 0,
            "unscorable_decision_count": 0,
            "per_decision_usdc": str(kwargs.get("per_decision_usdc")),
            "supported_parameter_keys": [],
            "results": [],
            "best_by_pnl": None,
            "best_by_win_rate": None,
        }


@pytest.fixture()
def http_client() -> tuple[TestClient, _RecordingService]:
    service = _RecordingService()
    app = FastAPI()
    app.include_router(markets_router)
    app.include_router(analytics_router)
    app.include_router(portfolio_router)
    app.include_router(audit_router)
    app.include_router(operations_router)
    app.dependency_overrides[get_admin_service] = lambda: service
    return TestClient(app), service


# ----------------------- /markets/{condition_id}/settlement --------------------


def test_market_settlement_404_when_missing(http_client: tuple[TestClient, _RecordingService]) -> None:
    client, service = http_client
    service.settlement_payload = None
    response = client.get("/markets/cond-X/settlement")
    assert response.status_code == 404
    assert response.json()["detail"] == "settlement_not_found"


def test_market_settlement_200_when_found(http_client: tuple[TestClient, _RecordingService]) -> None:
    client, service = http_client
    service.settlement_payload = {
        "condition_id": "cond-X",
        "winning_token_id": "tok-yes",
        "winning_outcome": "Yes",
        "settled_at": "2026-05-12T00:00:00+00:00",
        "source": "manual",
        "operator": "agent",
        "last_decision": {
            "record_id": "r1",
            "token_id": "tok-yes",
            "fair_value": "0.40",
            "outcome_minus_fair": "0.60",
        },
    }
    response = client.get("/markets/cond-X/settlement")
    assert response.status_code == 200
    body = response.json()
    assert body["last_decision"]["outcome_minus_fair"] == "0.60"


# --------------------- /analytics/missed-opportunities -------------------------


def test_missed_opportunities_converts_per_decision_usdc(
    http_client: tuple[TestClient, _RecordingService],
) -> None:
    client, service = http_client
    response = client.get(
        "/analytics/missed-opportunities",
        params={"per_decision_usdc": 25.5, "limit": 50, "since": 100, "until": 500},
    )
    assert response.status_code == 200
    kwargs = service.calls["missed_opportunities_snapshot"]
    assert kwargs["per_decision_usdc"] == Decimal("25.5")
    assert kwargs["limit"] == 50
    tr = kwargs["time_range"]
    assert tr is not None and (tr.since_ms, tr.until_ms) == (100, 500)


# ----------------------------- /portfolio/risk-metrics -------------------------


def test_risk_metrics_route_passes_params(http_client: tuple[TestClient, _RecordingService]) -> None:
    client, service = http_client
    response = client.get(
        "/portfolio/risk-metrics",
        params={"window_ms": 86400000, "interval_ms": 3600000, "annualization_factor": 365},
    )
    assert response.status_code == 200
    kwargs = service.calls["portfolio_risk_metrics"]
    assert kwargs["window_ms"] == 86400000
    assert kwargs["interval_ms"] == 3600000
    assert kwargs["annualization_factor"] == 365.0


def test_risk_metrics_route_503_when_db_unavailable(
    http_client: tuple[TestClient, _RecordingService],
) -> None:
    client, service = http_client
    service.risk_metrics_raises = True
    response = client.get("/portfolio/risk-metrics")
    assert response.status_code == 503


# ----------------------------- /audit-events/operators -------------------------


def test_operator_aggregation_route_no_filter(http_client: tuple[TestClient, _RecordingService]) -> None:
    client, service = http_client
    response = client.get("/audit-events/operators")
    assert response.status_code == 200
    kwargs = service.calls["aggregate_operator_interventions"]
    assert kwargs["operator"] is None


def test_operator_aggregation_route_with_filter(http_client: tuple[TestClient, _RecordingService]) -> None:
    client, service = http_client
    response = client.get(
        "/audit-events/operators",
        params={"operator": "agent", "sample_limit": 500, "since": 1000, "until": 2000},
    )
    assert response.status_code == 200
    kwargs = service.calls["aggregate_operator_interventions"]
    assert kwargs["operator"] == "agent"
    assert kwargs["sample_limit"] == 500
    tr = kwargs["time_range"]
    assert tr is not None and (tr.since_ms, tr.until_ms) == (1000, 2000)


# ----------------------------- /operations/parameter-sweep ---------------------


def test_parameter_sweep_route_passes_candidates(
    http_client: tuple[TestClient, _RecordingService],
) -> None:
    client, service = http_client
    response = client.post(
        "/operations/parameter-sweep",
        json={
            "candidates": {
                "tail_outright_min_edge_bps": [100, 500],
                "tail_outright_max_entry_price": [0.5, 0.9],
            },
            "per_decision_usdc": 20,
            "strategy_id": "sports_tail",
            "since": 100,
            "until": 500,
        },
    )
    assert response.status_code == 200
    kwargs = service.calls["run_parameter_sweep"]
    assert kwargs["candidates"]["tail_outright_min_edge_bps"] == [100, 500]
    assert kwargs["per_decision_usdc"] == Decimal("20")
    assert kwargs["strategy_id"] == "sports_tail"
    tr = kwargs["time_range"]
    assert tr is not None and (tr.since_ms, tr.until_ms) == (100, 500)


def test_parameter_sweep_route_422_on_unsupported_key(
    http_client: tuple[TestClient, _RecordingService],
) -> None:
    client, _ = http_client
    response = client.post(
        "/operations/parameter-sweep",
        json={"candidates": {"bogus_param": [1, 2]}},
    )
    assert response.status_code == 422
    detail = response.json()["detail"]
    # detail 不再透传 ValueError 文本（防信息泄漏）；改为固定 reason code + trace_id
    # 兜底反查（§10 可审计性）。
    assert detail["reason"] == "parameter_sweep_validation_failed"
    assert isinstance(detail.get("trace_id"), str) and len(detail["trace_id"]) > 0
