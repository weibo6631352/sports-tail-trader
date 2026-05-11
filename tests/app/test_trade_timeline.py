"""``build_trade_timeline`` 聚合行为 + ``GET /trades/{condition_id}/timeline`` 路由透传。

覆盖：
- 多张表事件按 ``timestamp`` 升序合并
- ``token_id`` 过滤
- ``limit`` 裁剪保留尾部（"末端发生了什么"）
- ``current_position`` 投影
- 路由把 ``since`` / ``until`` 转为 ``TimeRange`` 并透传
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from decimal import Decimal
from typing import Any

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from polymarket_trader.api.deps import get_admin_service
from polymarket_trader.api.routes.trades import router as trades_router
from polymarket_trader.app.admin_serialization import AdminSerializer
from polymarket_trader.app.trade_timeline import TradeTimelineInputs, build_trade_timeline
from polymarket_trader.domain.account import AccountSnapshot
from polymarket_trader.domain.decisions import DecisionRecord
from polymarket_trader.domain.events import AuditEvent, Fill, OutboxEvent
from polymarket_trader.domain.order import (
    Order,
    OrderSide,
    OrderStatus,
    OrderType,
)
from polymarket_trader.domain.position import Position
from polymarket_trader.runtime.registry import MarketRegistrySnapshot


BASE = datetime(2026, 5, 11, 12, 0, 0, tzinfo=timezone.utc)


def _serializer() -> AdminSerializer:
    return AdminSerializer(
        account_snapshot_provider=lambda: AccountSnapshot.empty(),
        registry_snapshot_provider=lambda: MarketRegistrySnapshot(markets=()),
        market_ws_snapshot=lambda token_id: None,
    )


def _decision(*, condition_id: str, token_id: str | None, offset_ms: int, accepted: bool) -> DecisionRecord:
    return DecisionRecord(
        strategy_id="sports_tail",
        trace_id=f"trace-{offset_ms}",
        condition_id=condition_id,
        token_id=token_id,
        decision_input={"market": condition_id},
        decision_output={"fair_value": "0.42", "kelly_fraction": "0.05"},
        accepted=accepted,
        reason=None if accepted else "no_signal",
        hook_name="decide_entry",
        created_at=BASE + timedelta(milliseconds=offset_ms),
    )


def _order(*, condition_id: str, token_id: str, offset_ms: int, status: OrderStatus = OrderStatus.SUBMITTED) -> Order:
    return Order(
        strategy_id="sports_tail",
        trace_id=f"trace-{offset_ms}",
        condition_id=condition_id,
        token_id=token_id,
        side=OrderSide.BUY,
        order_type=OrderType.FAK,
        price=Decimal("0.35"),
        amount_usdc=Decimal("10"),
        filled_shares=Decimal("0"),
        status=status,
        order_id=f"oid-{offset_ms}",
        created_at=BASE + timedelta(milliseconds=offset_ms),
        updated_at=BASE + timedelta(milliseconds=offset_ms),
    )


def _fill(*, condition_id: str, token_id: str, offset_ms: int, side: str = "buy") -> Fill:
    return Fill(
        strategy_id="sports_tail",
        trace_id=f"trace-{offset_ms}",
        event_type="trade",
        event_id=f"fill-{offset_ms}",
        condition_id=condition_id,
        token_id=token_id,
        order_id=f"oid-{offset_ms}",
        side=side,
        price=Decimal("0.35"),
        size=Decimal("10"),
        notional_usdc=Decimal("3.5"),
        confirmed_at=BASE + timedelta(milliseconds=offset_ms),
    )


def _audit(*, condition_id: str, token_id: str | None, offset_ms: int, title: str) -> AuditEvent:
    return AuditEvent(
        strategy_id="sports_tail",
        trace_id=f"trace-{offset_ms}",
        event_id=f"audit-{offset_ms}",
        event_title=title,
        condition_id=condition_id,
        token_id=token_id,
        status="ok",
        reason="ok",
        payload={},
        created_at=BASE + timedelta(milliseconds=offset_ms),
    )


def _outbox(*, condition_id: str, token_id: str | None, offset_ms: int, event_type: str) -> OutboxEvent:
    return OutboxEvent(
        trace_id=f"trace-{offset_ms}",
        event_type=event_type,
        idempotency_key=f"idem-{offset_ms}",
        event_id=f"out-{offset_ms}",
        condition_id=condition_id,
        token_id=token_id,
        reason="reason",
        created_at=BASE + timedelta(milliseconds=offset_ms),
        priority=3,
        payload={"action_type": "cancel_order"},
    )


def _position(*, condition_id: str, token_id: str) -> Position:
    return Position(
        strategy_id="sports_tail",
        condition_id=condition_id,
        token_id=token_id,
        shares=Decimal("10"),
        cost_usdc=Decimal("3.5"),
        avg_price=Decimal("0.35"),
        cur_price=Decimal("0.45"),
        realized_pnl=Decimal("0"),
        cash_pnl=Decimal("1"),
        updated_at=BASE + timedelta(milliseconds=1000),
    )


def test_timeline_merges_events_in_chronological_order() -> None:
    cid = "cond-X"
    tid = "tok-X"
    inputs = TradeTimelineInputs(
        decisions=(_decision(condition_id=cid, token_id=tid, offset_ms=100, accepted=True),),
        orders=(_order(condition_id=cid, token_id=tid, offset_ms=200),),
        fills=(_fill(condition_id=cid, token_id=tid, offset_ms=300),),
        audit_events=(_audit(condition_id=cid, token_id=tid, offset_ms=50, title="discovery"),),
        outbox_events=(_outbox(condition_id=cid, token_id=tid, offset_ms=400, event_type="reconcile_diff_detected"),),
        positions=(_position(condition_id=cid, token_id=tid),),
    )
    result = build_trade_timeline(
        condition_id=cid,
        token_id=tid,
        inputs=inputs,
        serializer=_serializer(),
        limit=1000,
    )
    kinds = [ev["kind"] for ev in result["events"]]
    assert kinds == ["audit", "decision", "order", "fill", "outbox"]
    assert result["truncated"] is False
    assert result["event_count"] == 5
    assert result["current_position"] is not None
    assert result["current_position"]["shares"] == "10"


def test_timeline_filters_by_token_id() -> None:
    cid = "cond-X"
    inputs = TradeTimelineInputs(
        decisions=(
            _decision(condition_id=cid, token_id="tok-A", offset_ms=100, accepted=True),
            _decision(condition_id=cid, token_id="tok-B", offset_ms=200, accepted=True),
            _decision(condition_id=cid, token_id=None, offset_ms=300, accepted=False),  # 无 token 决策
        ),
        orders=(),
        fills=(),
        audit_events=(),
        outbox_events=(),
        positions=(),
    )
    result = build_trade_timeline(
        condition_id=cid,
        token_id="tok-A",
        inputs=inputs,
        serializer=_serializer(),
        limit=1000,
    )
    # tok-A 决策 + 无 token 决策（token_id is None 视为整 condition 通用）
    tokens = [ev.get("token_id") for ev in result["events"]]
    assert "tok-B" not in tokens
    assert "tok-A" in tokens
    assert None in tokens


def test_timeline_truncates_keeping_tail() -> None:
    cid = "cond-X"
    decisions = tuple(
        _decision(condition_id=cid, token_id="tok-X", offset_ms=i, accepted=True)
        for i in range(10)
    )
    inputs = TradeTimelineInputs(
        decisions=decisions,
        orders=(),
        fills=(),
        audit_events=(),
        outbox_events=(),
        positions=(),
    )
    result = build_trade_timeline(
        condition_id=cid,
        token_id="tok-X",
        inputs=inputs,
        serializer=_serializer(),
        limit=3,
    )
    assert result["truncated"] is True
    # tail：保留最大 offset_ms 的 3 条 → offset 7/8/9
    offsets = [ev["trace_id"] for ev in result["events"]]
    assert offsets == ["trace-7", "trace-8", "trace-9"]


def test_timeline_filters_condition_id_strictly() -> None:
    inputs = TradeTimelineInputs(
        decisions=(
            _decision(condition_id="cond-A", token_id="tok-X", offset_ms=100, accepted=True),
            _decision(condition_id="cond-B", token_id="tok-X", offset_ms=200, accepted=True),
        ),
        orders=(),
        fills=(),
        audit_events=(),
        outbox_events=(),
        positions=(),
    )
    result = build_trade_timeline(
        condition_id="cond-A",
        token_id=None,
        inputs=inputs,
        serializer=_serializer(),
        limit=1000,
    )
    assert result["event_count"] == 1
    assert result["events"][0]["trace_id"] == "trace-100"


# --------------------------- /trades/{condition_id}/timeline -------------------


class _RecordingService:
    def __init__(self) -> None:
        self.kwargs: dict[str, Any] | None = None

    async def get_trade_timeline(self, **kwargs: Any) -> dict[str, Any]:
        self.kwargs = kwargs
        return {
            "condition_id": kwargs["condition_id"],
            "token_id": kwargs.get("token_id"),
            "event_count": 0,
            "truncated": False,
            "events": [],
            "current_position": None,
        }


@pytest.fixture()
def client() -> tuple[TestClient, _RecordingService]:
    service = _RecordingService()
    app = FastAPI()
    app.include_router(trades_router)
    app.dependency_overrides[get_admin_service] = lambda: service
    return TestClient(app), service


def test_trade_timeline_route_passes_filters(client: tuple[TestClient, _RecordingService]) -> None:
    test_client, service = client
    response = test_client.get(
        "/trades/cond-X/timeline",
        params={"token_id": "tok-Y", "since": 100, "until": 500, "limit": 50},
    )
    assert response.status_code == 200
    assert service.kwargs is not None
    assert service.kwargs["condition_id"] == "cond-X"
    assert service.kwargs["token_id"] == "tok-Y"
    assert service.kwargs["limit"] == 50
    tr = service.kwargs["time_range"]
    assert tr is not None and (tr.since_ms, tr.until_ms) == (100, 500)
