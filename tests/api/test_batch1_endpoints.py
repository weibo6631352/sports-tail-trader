"""Batch 1 新增 API 端点的路由 + 投影行为。

覆盖：
- ``GET /decisions/{record_id}`` —— 单条决策详情，404 / 200 + decision_output 投影
- ``GET /outbox/failures`` —— 路由参数透传到 AdminService.list_outbox_failures
- ``GET /markets/orderbook-history`` —— 422 (无 token/condition) + 200
- ``GET /metrics/latency-percentiles`` —— 路由参数透传 + ``_compute_latency_payload``
  的 p50/p90/p95/p99 数学正确性
- ``GET /operations/reconcile/diffs`` —— 路由参数透传 + include_started/applied 切换
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from typing import Any

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from polymarket_trader.api.deps import get_admin_service
from polymarket_trader.api.routes.markets import router as markets_router
from polymarket_trader.api.routes.operations import router as operations_router
from polymarket_trader.api.routes.outbox import router as outbox_router
from polymarket_trader.api.routes.runtime import router as runtime_router
from polymarket_trader.app.admin_query._helpers import (
    _compute_latency_payload,
    _percentile,
)


class _RecordingAdminService:
    """只记录调用参数；不实际访问 DB。"""

    def __init__(self) -> None:
        self.calls: dict[str, Any] = {}
        self.decision_payload: dict[str, Any] | None = None
        self.failures_payload: dict[str, Any] = {"items": [], "total": 0, "limit": 100, "offset": 0}
        self.orderbook_payload: dict[str, Any] = {"items": [], "total": 0, "limit": 200, "offset": 0}
        self.latency_payload: dict[str, Any] = {
            "window_ms": None,
            "sample_limit": 500,
            "event_types": [],
            "sample_count": 0,
            "stages": {},
        }
        self.reconcile_payload: dict[str, Any] = {"items": [], "total": 0, "limit": 100, "offset": 0}

    async def get_decision_record(self, record_id: str) -> dict[str, Any] | None:
        self.calls["get_decision_record"] = {"record_id": record_id}
        return self.decision_payload

    async def list_outbox_failures(self, **kwargs: Any) -> dict[str, Any]:
        self.calls["list_outbox_failures"] = kwargs
        return self.failures_payload

    async def list_orderbook_history(self, **kwargs: Any) -> dict[str, Any]:
        self.calls["list_orderbook_history"] = kwargs
        return self.orderbook_payload

    async def latency_percentiles_snapshot(self, **kwargs: Any) -> dict[str, Any]:
        self.calls["latency_percentiles_snapshot"] = kwargs
        return self.latency_payload

    async def list_reconcile_diffs(self, **kwargs: Any) -> dict[str, Any]:
        self.calls["list_reconcile_diffs"] = kwargs
        return self.reconcile_payload

    # runtime router 还需要这些方法 —— 测试里不会真用，但 Depends 不能抛错。
    async def runtime_snapshot(self) -> dict[str, Any]:
        return {}

    def workers_snapshot(self) -> dict[str, Any]:
        return {}

    def metrics_snapshot(self) -> dict[str, Any]:
        return {}


@pytest.fixture()
def client_with_admin() -> tuple[TestClient, _RecordingAdminService]:
    service = _RecordingAdminService()
    app = FastAPI()
    app.include_router(runtime_router)
    app.include_router(markets_router)
    app.include_router(outbox_router)
    app.include_router(operations_router)
    app.dependency_overrides[get_admin_service] = lambda: service
    return TestClient(app), service


# --------------------------- /decisions/{record_id} ----------------------------


def test_decision_detail_returns_404_when_missing(
    client_with_admin: tuple[TestClient, _RecordingAdminService],
) -> None:
    client, service = client_with_admin
    service.decision_payload = None
    response = client.get("/decisions/missing-id")
    assert response.status_code == 404
    assert response.json()["detail"] == "decision_record_not_found"
    assert service.calls["get_decision_record"] == {"record_id": "missing-id"}


def test_decision_detail_returns_payload_with_jsonb(
    client_with_admin: tuple[TestClient, _RecordingAdminService],
) -> None:
    client, service = client_with_admin
    service.decision_payload = {
        "record_id": "rid-1",
        "trace_id": "trace-1",
        "hook_name": "decide_entry",
        "decision_input": {"market_slug": "x"},
        "decision_output": {
            "fair_value": "0.42",
            "entry_price_cap": "0.38",
            "kelly_fraction": "0.05",
        },
        "accepted": True,
        "reason": None,
        "created_at": "2026-05-11T00:00:00+00:00",
    }
    response = client.get("/decisions/rid-1")
    assert response.status_code == 200
    body = response.json()
    assert body["decision_output"]["fair_value"] == "0.42"
    assert body["decision_output"]["kelly_fraction"] == "0.05"


# ------------------------------- /outbox/failures ------------------------------


def test_outbox_failures_passes_filters(
    client_with_admin: tuple[TestClient, _RecordingAdminService],
) -> None:
    client, service = client_with_admin
    response = client.get(
        "/outbox/failures",
        params={
            "limit": 25,
            "offset": 7,
            "trace_id": "trace-x",
            "event_type": "order_submitted",
            "since": 100,
            "until": 200,
            "min_retry_count": 3,
        },
    )
    assert response.status_code == 200
    kwargs = service.calls["list_outbox_failures"]
    assert kwargs["limit"] == 25
    assert kwargs["offset"] == 7
    assert kwargs["trace_id"] == "trace-x"
    assert kwargs["event_type"] == "order_submitted"
    assert kwargs["min_retry_count"] == 3
    time_range = kwargs["time_range"]
    assert time_range is not None and (time_range.since_ms, time_range.until_ms) == (100, 200)


# --------------------------- /markets/orderbook-history ------------------------


def test_orderbook_history_requires_token_or_condition(
    client_with_admin: tuple[TestClient, _RecordingAdminService],
) -> None:
    client, _ = client_with_admin
    response = client.get("/markets/orderbook-history")
    assert response.status_code == 422
    assert response.json()["detail"] == "token_id_or_condition_id_required"


def test_orderbook_history_passes_token_and_time_range(
    client_with_admin: tuple[TestClient, _RecordingAdminService],
) -> None:
    client, service = client_with_admin
    response = client.get(
        "/markets/orderbook-history",
        params={"token_id": "tok-1", "since": 1000, "until": 2000, "limit": 50},
    )
    assert response.status_code == 200
    kwargs = service.calls["list_orderbook_history"]
    assert kwargs["token_id"] == "tok-1"
    assert kwargs["condition_id"] is None
    assert kwargs["limit"] == 50
    tr = kwargs["time_range"]
    assert tr is not None and (tr.since_ms, tr.until_ms) == (1000, 2000)


# ------------------------- /metrics/latency-percentiles ------------------------


def test_latency_percentiles_route_passes_params(
    client_with_admin: tuple[TestClient, _RecordingAdminService],
) -> None:
    client, service = client_with_admin
    response = client.get(
        "/metrics/latency-percentiles", params={"window_ms": 60000, "sample_limit": 1000}
    )
    assert response.status_code == 200
    kwargs = service.calls["latency_percentiles_snapshot"]
    assert kwargs == {"window_ms": 60000, "sample_limit": 1000}


def test_percentile_linear_interpolation() -> None:
    """``_percentile`` 与 numpy.percentile linear 同义；这里对 N=5 样本验证关键点。"""

    values = [10.0, 20.0, 30.0, 40.0, 50.0]
    assert _percentile(values, 0.0) == 10.0
    assert _percentile(values, 1.0) == 50.0
    assert _percentile(values, 0.5) == 30.0
    # pos = 0.9 * 4 = 3.6 → 40 + 0.6*(50-40) = 46
    assert _percentile(values, 0.9) == pytest.approx(46.0)
    # pos = 0.95 * 4 = 3.8 → 40 + 0.8*(50-40) = 48
    assert _percentile(values, 0.95) == pytest.approx(48.0)


class _FakeEvent:
    def __init__(self, payload: dict[str, Any]) -> None:
        self.payload = payload


def test_compute_latency_payload_aggregates_per_stage() -> None:
    base = datetime(2026, 5, 11, 12, 0, 0, tzinfo=timezone.utc)

    def _event(queue_to_ack_ms: int) -> _FakeEvent:
        queued = base
        signed = base + timedelta(milliseconds=queue_to_ack_ms // 4)
        submitted = base + timedelta(milliseconds=queue_to_ack_ms // 2)
        ack = base + timedelta(milliseconds=queue_to_ack_ms)
        return _FakeEvent(
            {
                "timestamps": {
                    "queued_at": queued.isoformat(),
                    "sign_started_at": queued.isoformat(),
                    "signed_at": signed.isoformat(),
                    "submitted_at": submitted.isoformat(),
                    "ack_at": ack.isoformat(),
                }
            }
        )

    events = tuple(_event(ms) for ms in (100, 200, 300, 400, 500))
    payload = _compute_latency_payload(events, ("order_submitted",), 100, 60000)
    stages = payload["stages"]
    assert stages["queue_to_ack"]["count"] == 5
    # 5 个样本 100/200/300/400/500，p50 = 300
    assert stages["queue_to_ack"]["percentiles_ms"]["p50"] == pytest.approx(300.0)
    assert stages["queue_to_ack"]["min_ms"] == 100.0
    assert stages["queue_to_ack"]["max_ms"] == 500.0
    # queue_to_sign 是 25/50/75/100/125
    assert stages["queue_to_sign"]["percentiles_ms"]["p50"] == pytest.approx(75.0)


def test_compute_latency_payload_skips_missing_or_inverted_timestamps() -> None:
    base = datetime(2026, 5, 11, 12, 0, 0, tzinfo=timezone.utc)
    later = base + timedelta(milliseconds=100)
    events = (
        _FakeEvent({"timestamps": {}}),  # 全部 None
        _FakeEvent({"timestamps": {"queued_at": later.isoformat(), "ack_at": base.isoformat()}}),  # 倒序
        _FakeEvent({"timestamps": {"queued_at": base.isoformat(), "ack_at": later.isoformat()}}),  # 正向
        _FakeEvent({"timestamps": None}),  # type: ignore[dict-item]
    )
    payload = _compute_latency_payload(events, ("order_submitted",), 100, None)
    # 只有 1 个事件给出有效的 queue_to_ack；倒序被丢弃，None payload 被丢弃
    assert payload["stages"]["queue_to_ack"]["count"] == 1
    assert payload["sample_count"] == 4  # 总取样数即使无效也保留


# --------------------------- /operations/reconcile/diffs -----------------------


def test_reconcile_diffs_default_includes_applied_excludes_started(
    client_with_admin: tuple[TestClient, _RecordingAdminService],
) -> None:
    client, service = client_with_admin
    response = client.get("/operations/reconcile/diffs", params={"limit": 30, "offset": 2})
    assert response.status_code == 200
    kwargs = service.calls["list_reconcile_diffs"]
    assert kwargs["limit"] == 30
    assert kwargs["offset"] == 2
    assert kwargs["include_applied"] is True
    assert kwargs["include_started"] is False


def test_reconcile_diffs_passes_full_filter_set(
    client_with_admin: tuple[TestClient, _RecordingAdminService],
) -> None:
    client, service = client_with_admin
    response = client.get(
        "/operations/reconcile/diffs",
        params={
            "trace_id": "trace-r",
            "condition_id": "cond-r",
            "since": 50,
            "until": 150,
            "include_started": "true",
            "include_applied": "false",
        },
    )
    assert response.status_code == 200
    kwargs = service.calls["list_reconcile_diffs"]
    assert kwargs["trace_id"] == "trace-r"
    assert kwargs["condition_id"] == "cond-r"
    assert kwargs["include_started"] is True
    assert kwargs["include_applied"] is False
    tr = kwargs["time_range"]
    assert tr is not None and (tr.since_ms, tr.until_ms) == (50, 150)
