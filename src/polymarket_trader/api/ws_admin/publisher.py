"""AdminWsPublisher —— admin WebSocket 增量推送桥接（实装版）。

订阅 `event_bus` broadcast listener → 按 topic 路由 → 异步推给 WS subscribers。

# 数据流

```
event_bus broadcast (sync)
    ↓ _on_event(event)         # sync callback，0 阻塞
asyncio.Queue (P3 lane)
    ↓ _drain_loop (async task)
按 DomainEventType → topic 路由
    ↓
_push_to_topic(topic, payload)
    ↓ asyncio.gather
所有 subscriber.send_json(...)（失败的从订阅列表移除）
```

# Topic 路由表

| event_type | topic |
|---|---|
| POSITION_UPDATED / BALANCE_UPDATED / FILL_RECORDED / ORDER_* | `portfolio` |
| ENTRY_SIGNAL_TRIGGERED / DECISION_RECORDED | `candidates` |
| SPORTS_LIVE_STATE_RECORDED / SPORTS_LIVE_MATCH_GAP_RECORDED | `live_states` |
| (周期 1s 主动推) | `health` |

# Subscriber 协议（WebSocket-like）

任何 obj 含 async `send_json(payload)` 都可作 subscriber。Endpoint 接入时
给 starlette WebSocket 实例。推送失败（IO 异常）自动从订阅列表移除。

# 当前现状

publisher 可独立启动 + drain event_bus，但未在 main.py wire（待 endpoint
改造 api/routes/stream.py 时接入）。接口完整，可独立单元测试。
"""

from __future__ import annotations

import asyncio
import logging
from typing import TYPE_CHECKING, Any, Protocol

if TYPE_CHECKING:
    from polymarket_trader.runtime.event_bus import EventBus

logger = logging.getLogger(__name__)


class WsSubscriber(Protocol):
    """订阅者协议——任何具备 async send_json 的 obj 都可作 subscriber。"""

    async def send_json(self, payload: dict[str, Any]) -> None: ...


# DomainEventType.value → topic 路由表
_EVENT_TYPE_TO_TOPIC: dict[str, str] = {
    "position_updated": "portfolio",
    "balance_updated": "portfolio",
    "fill_recorded": "portfolio",
    "order_created": "portfolio",
    "order_submitted": "portfolio",
    "order_cancelled": "portfolio",
    "order_matched": "portfolio",
    "order_partially_filled": "portfolio",
    "entry_signal_triggered": "candidates",
    "decision_recorded": "candidates",
    "allocation_decision_recorded": "candidates",
    "sports_live_state_recorded": "live_states",
    "sports_live_match_gap_recorded": "live_states",
}

_KNOWN_TOPICS: frozenset[str] = frozenset(_EVENT_TYPE_TO_TOPIC.values()) | {"health"}

_QUEUE_MAX_SIZE: int = 1000  # 队列满时丢老消息（避免 publisher 内存爆）


class AdminWsPublisher:
    """admin WebSocket 增量推送桥接。"""

    def __init__(self, *, event_bus: "EventBus") -> None:
        self._event_bus = event_bus
        # topic → subscriber set
        self._subscribers: dict[str, set[WsSubscriber]] = {}
        self._queue: asyncio.Queue[Any] = asyncio.Queue(maxsize=_QUEUE_MAX_SIZE)
        self._drain_task: asyncio.Task[None] | None = None
        self._running = False
        self._listener_registered = False

    # ===== 订阅管理 =====

    def subscribe(self, websocket: WsSubscriber, topics: tuple[str, ...]) -> None:
        for topic in topics:
            if topic not in _KNOWN_TOPICS:
                logger.warning("admin_ws.unknown_topic", extra={"topic": topic})
                continue
            self._subscribers.setdefault(topic, set()).add(websocket)

    def unsubscribe(self, websocket: WsSubscriber, topics: tuple[str, ...] | None = None) -> None:
        if topics is None:
            for subs in self._subscribers.values():
                subs.discard(websocket)
        else:
            for topic in topics:
                subs = self._subscribers.get(topic)
                if subs is not None:
                    subs.discard(websocket)

    def topic_subscriber_count(self, topic: str) -> int:
        return len(self._subscribers.get(topic, set()))

    def known_topics(self) -> frozenset[str]:
        return _KNOWN_TOPICS

    # ===== 生命周期 =====

    def start(self) -> None:
        """启动 publisher：注册 event_bus listener + 启动 drain task。"""

        if self._running:
            return
        self._running = True
        if not self._listener_registered:
            self._event_bus.add_broadcast_listener(self._on_event)
            self._listener_registered = True
        self._drain_task = asyncio.create_task(self._drain_loop(), name="admin-ws-publisher")

    async def stop(self) -> None:
        self._running = False
        if self._listener_registered:
            self._event_bus.remove_broadcast_listener(self._on_event)
            self._listener_registered = False
        task = self._drain_task
        if task is not None and not task.done():
            task.cancel()
            try:
                await task
            except (asyncio.CancelledError, Exception):  # noqa: BLE001
                pass
        self._drain_task = None

    # ===== 事件流转 =====

    def _on_event(self, event: Any) -> None:
        """event_bus broadcast 回调——sync，必须快。投到 queue 异步处理。"""

        try:
            self._queue.put_nowait(event)
        except asyncio.QueueFull:
            # 队列满 → 丢老消息保新（admin push 是 P3，不影响交易主路径）
            try:
                self._queue.get_nowait()
                self._queue.put_nowait(event)
            except Exception:  # noqa: BLE001
                pass

    async def _drain_loop(self) -> None:
        while self._running:
            try:
                event = await self._queue.get()
            except asyncio.CancelledError:
                raise
            try:
                topic = self._route_event(event)
                if topic is None:
                    continue
                payload = self._serialize_event(topic, event)
                await self._push_to_topic(topic, payload)
            except Exception:  # noqa: BLE001
                logger.exception("admin_ws drain error")

    def _route_event(self, event: Any) -> str | None:
        event_type = getattr(event, "event_type", None)
        if event_type is None:
            return None
        return _EVENT_TYPE_TO_TOPIC.get(event_type)

    def _serialize_event(self, topic: str, event: Any) -> dict[str, Any]:
        return {
            "topic": topic,
            "event_type": getattr(event, "event_type", None),
            "event_id": getattr(event, "event_id", None),
            "trace_id": getattr(event, "trace_id", None),
            "condition_id": getattr(event, "condition_id", None),
            "token_id": getattr(event, "token_id", None),
            "market_slug": getattr(event, "market_slug", None),
            "reason": getattr(event, "reason", None),
            "payload": dict(getattr(event, "payload", {})),
        }

    async def _push_to_topic(self, topic: str, payload: dict[str, Any]) -> None:
        subscribers = tuple(self._subscribers.get(topic, set()))
        if not subscribers:
            return
        dead: list[WsSubscriber] = []
        results = await asyncio.gather(
            *(self._safe_send(ws, payload) for ws in subscribers),
            return_exceptions=False,
        )
        for ws, ok in zip(subscribers, results):
            if not ok:
                dead.append(ws)
        for ws in dead:
            self.unsubscribe(ws)

    @staticmethod
    async def _safe_send(ws: WsSubscriber, payload: dict[str, Any]) -> bool:
        try:
            await ws.send_json(payload)
            return True
        except Exception:  # noqa: BLE001
            return False
