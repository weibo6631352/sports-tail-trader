"""GET /stream/events 的端到端覆盖。

测试策略：
- 端点级别（429 + 422）：通过 httpx ASGITransport 同步触发，立即返回。
- 流式行为（订阅 + 过滤 + lag + 心跳）：直接驱动 ``iter_listener_frames``，
  避免 ASGITransport 在 StreamingResponse 上的已知缓冲限制。``broadcast`` 与
  ``register/unregister`` 在路由和测试里走同一条代码路径，覆盖一致。
"""

from __future__ import annotations

import asyncio
import json
from types import SimpleNamespace
from typing import Any

import httpx
import pytest
from fastapi import FastAPI

from polymarket_trader.api.routes.stream import (
    LISTENER_QUEUE_MAXSIZE,
    SseSubscriptionRegistry,
    _LagMarker,
    iter_listener_frames,
    router,
)


def _make_app(registry: SseSubscriptionRegistry) -> FastAPI:
    app = FastAPI()
    runtime = SimpleNamespace(sse_subscription_registry=registry)
    app.state.runtime = runtime
    app.state.get_runtime = lambda: runtime
    app.include_router(router)
    return app


def _domain_event(
    *,
    event_id: str,
    event_type: str = "trade_confirmed",
    condition_id: str | None = None,
) -> SimpleNamespace:
    return SimpleNamespace(
        event_id=event_id,
        event_type=event_type,
        trace_id=f"trace-{event_id}",
        condition_id=condition_id,
        market_slug=None,
        as_dict=lambda: {
            "event_id": event_id,
            "event_type": event_type,
            "trace_id": f"trace-{event_id}",
            "condition_id": condition_id,
        },
    )


def _parse_frame(raw: bytes) -> tuple[str | None, dict[str, Any] | None, bool]:
    text = raw.decode("utf-8")
    if text.startswith(":"):
        return None, None, True
    event_name: str | None = None
    data_line: str | None = None
    for line in text.splitlines():
        if line.startswith("event: "):
            event_name = line[len("event: "):].strip()
        elif line.startswith("data: "):
            data_line = line[len("data: "):]
    payload = json.loads(data_line) if data_line else None
    return event_name, payload, False


@pytest.mark.asyncio
async def test_connect_and_receive_event_publishes_payload() -> None:
    registry = SseSubscriptionRegistry()
    listener = registry.register(event_types=None, condition_id=None)
    gen = iter_listener_frames(listener, heartbeat_seconds=60.0)
    try:
        ready = await anext(gen)
        name, payload, _ = _parse_frame(ready)
        assert name == "ready"
        assert payload is not None and payload["kind"] == "ready"

        registry.broadcast(_domain_event(event_id="evt-1"))
        frame = await asyncio.wait_for(anext(gen), timeout=1.0)
        _, payload, _ = _parse_frame(frame)
        assert payload is not None
        assert payload["event_id"] == "evt-1"
        assert payload["event_type"] == "trade_confirmed"
    finally:
        await gen.aclose()
        registry.unregister(listener)


@pytest.mark.asyncio
async def test_disconnect_cleanup_unregisters_listener() -> None:
    registry = SseSubscriptionRegistry()
    listener = registry.register(event_types=None, condition_id=None)
    assert registry.active_count() == 1

    gen = iter_listener_frames(listener, heartbeat_seconds=60.0)
    await anext(gen)  # ready
    # 模拟 HTTP 端 finally 块的清理逻辑（与路由保持一致）。
    await gen.aclose()
    registry.unregister(listener)
    assert registry.active_count() == 0


@pytest.mark.asyncio
async def test_event_type_filter_drops_other_types() -> None:
    registry = SseSubscriptionRegistry()
    listener = registry.register(event_types=["trade_confirmed"], condition_id=None)
    gen = iter_listener_frames(listener, heartbeat_seconds=60.0, include_ready=False)
    try:
        registry.broadcast(_domain_event(event_id="skip", event_type="other"))
        registry.broadcast(_domain_event(event_id="keep", event_type="trade_confirmed"))
        frame = await asyncio.wait_for(anext(gen), timeout=1.0)
        _, payload, _ = _parse_frame(frame)
        assert payload is not None and payload["event_id"] == "keep"
        # 队列里不应再有 skip 事件。
        assert listener.queue.empty()
    finally:
        await gen.aclose()
        registry.unregister(listener)


@pytest.mark.asyncio
async def test_condition_id_filter_drops_other_markets() -> None:
    registry = SseSubscriptionRegistry()
    listener = registry.register(event_types=None, condition_id="cond-A")
    gen = iter_listener_frames(listener, heartbeat_seconds=60.0, include_ready=False)
    try:
        registry.broadcast(_domain_event(event_id="other", condition_id="cond-B"))
        registry.broadcast(_domain_event(event_id="match", condition_id="cond-A"))
        frame = await asyncio.wait_for(anext(gen), timeout=1.0)
        _, payload, _ = _parse_frame(frame)
        assert payload is not None and payload["event_id"] == "match"
        assert listener.queue.empty()
    finally:
        await gen.aclose()
        registry.unregister(listener)


@pytest.mark.asyncio
async def test_burst_overflow_emits_subscription_lag_once_per_batch() -> None:
    registry = SseSubscriptionRegistry()
    listener = registry.register(event_types=None, condition_id=None)
    try:
        burst = LISTENER_QUEUE_MAXSIZE + 5
        for index in range(burst):
            registry.broadcast(_domain_event(event_id=f"e-{index}"))
        assert listener.queue.qsize() == LISTENER_QUEUE_MAXSIZE
        assert registry.dropped_events_total() >= 5

        seen_lag = 0
        seen_events = 0
        while not listener.queue.empty():
            item = listener.queue.get_nowait()
            if isinstance(item, _LagMarker):
                seen_lag += 1
            else:
                seen_events += 1
        assert seen_lag == 1
        assert seen_events == LISTENER_QUEUE_MAXSIZE - 1
    finally:
        registry.unregister(listener)


@pytest.mark.asyncio
async def test_burst_yields_subscription_lag_frame_in_stream() -> None:
    registry = SseSubscriptionRegistry()
    listener = registry.register(event_types=None, condition_id=None)
    try:
        burst = LISTENER_QUEUE_MAXSIZE + 3
        for index in range(burst):
            registry.broadcast(_domain_event(event_id=f"e-{index}"))

        gen = iter_listener_frames(listener, heartbeat_seconds=60.0, include_ready=False)
        try:
            saw_lag = False
            saw_event = False
            # 至多遍历队列里所有 items；lag marker 必须在某个位置出现一次。
            for _ in range(LISTENER_QUEUE_MAXSIZE + 5):
                if listener.queue.empty():
                    break
                frame = await asyncio.wait_for(anext(gen), timeout=1.0)
                name, payload, _ = _parse_frame(frame)
                if name == "subscription_lag":
                    saw_lag = True
                    assert payload is not None and payload["dropped"] >= 1
                elif payload is not None and payload.get("event_id", "").startswith("e-"):
                    saw_event = True
                if saw_lag and saw_event:
                    break
            assert saw_lag, "subscription_lag SSE frame should be emitted"
            assert saw_event, "real events should still flow alongside lag marker"
        finally:
            await gen.aclose()
    finally:
        registry.unregister(listener)


@pytest.mark.asyncio
async def test_heartbeat_custom_interval_emits_comment_frame() -> None:
    registry = SseSubscriptionRegistry()
    listener = registry.register(event_types=None, condition_id=None)
    gen = iter_listener_frames(
        listener,
        heartbeat_seconds=0.05,  # 仅在测试内夹紧，验证 timeout 心跳路径。
        include_ready=False,
    )
    try:
        frame = await asyncio.wait_for(anext(gen), timeout=1.0)
        _, _, is_heartbeat = _parse_frame(frame)
        assert is_heartbeat, "expected SSE comment heartbeat frame after timeout"
    finally:
        await gen.aclose()
        registry.unregister(listener)


@pytest.mark.asyncio
async def test_ready_frame_reports_heartbeat_ms() -> None:
    registry = SseSubscriptionRegistry()
    listener = registry.register(event_types=None, condition_id=None)
    gen = iter_listener_frames(listener, heartbeat_seconds=5.0)
    try:
        ready = await anext(gen)
        name, payload, _ = _parse_frame(ready)
        assert name == "ready"
        assert payload is not None and payload["heartbeat_ms"] == 5000
    finally:
        await gen.aclose()
        registry.unregister(listener)


@pytest.mark.asyncio
async def test_subscriber_cap_returns_429_with_retry_after() -> None:
    registry = SseSubscriptionRegistry(soft_cap=1)
    standing = registry.register(event_types=None, condition_id=None)
    try:
        app = _make_app(registry)
        transport = httpx.ASGITransport(app=app)
        async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
            response = await client.get("/stream/events")
        assert response.status_code == 429
        assert response.headers.get("Retry-After") == "5"
    finally:
        registry.unregister(standing)


@pytest.mark.asyncio
async def test_heartbeat_below_min_returns_422() -> None:
    registry = SseSubscriptionRegistry()
    app = _make_app(registry)
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        response = await client.get("/stream/events", params={"heartbeat_ms": 100})
    assert response.status_code == 422


@pytest.mark.asyncio
async def test_event_bus_broadcast_listener_fanout_integration() -> None:
    """端到端：EventBus.publish -> add_broadcast_listener -> registry.broadcast 闭环。"""

    from polymarket_trader.domain.events import OutboxPriority
    from polymarket_trader.runtime.event_bus import EventBus

    bus = EventBus(trading_capacity=16, maintenance_capacity=16, persistence_capacity=16)
    registry = SseSubscriptionRegistry()
    bus.add_broadcast_listener(registry.broadcast)
    listener = registry.register(event_types=None, condition_id=None)
    try:
        event = _domain_event(event_id="bus-1", event_type="user_event")
        # 用 P2 lane（maintenance），跳过 trading lane 的 merge_key 行为简化测试。
        await bus.publish(OutboxPriority.P2, event)
        # broadcast 是同步的，event 应该已经进入 listener.queue。
        assert listener.queue.qsize() == 1
        item = listener.queue.get_nowait()
        assert getattr(item, "event_id", None) == "bus-1"
    finally:
        registry.unregister(listener)
        bus.remove_broadcast_listener(registry.broadcast)


@pytest.mark.asyncio
async def test_event_bus_broadcast_swallows_listener_exception() -> None:
    """监听器异常不允许冒出到 publish；旁路绝不阻断主链路。"""

    from polymarket_trader.domain.events import OutboxPriority
    from polymarket_trader.runtime.event_bus import EventBus

    bus = EventBus(trading_capacity=16, maintenance_capacity=16, persistence_capacity=16)

    def explode(_event: Any) -> None:
        raise RuntimeError("listener boom")

    bus.add_broadcast_listener(explode)
    try:
        # publish 必须正常返回。
        await bus.publish(OutboxPriority.P2, _domain_event(event_id="x"))
    finally:
        bus.remove_broadcast_listener(explode)
