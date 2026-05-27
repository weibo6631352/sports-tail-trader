"""SSE fan-out endpoint for read-only audit/event subscribers.

设计要点（与 CLAUDE.md §3、§7 对齐）：

- 完全只读旁路。EventBus.publish 走 `add_broadcast_listener` 同步回调，
  常态下无订阅者直接 O(1) 返回；订阅者发生异常或队列满，全部由本模块吞掉，
  绝不反向阻塞交易热路径。
- 每个 listener 一条 ``asyncio.Queue(maxsize=1000)``。满队时丢最旧（drop-oldest）
  并以一条 ``subscription_lag`` marker 入队，告诉订阅方有跳过；同一批连续溢出
  只标记一次，避免 lag marker 自己又把队列灌满。
- 订阅软上限由 ``Settings.sse_subscriber_cap`` 控制（默认 32），通过 main.py
  在构造 ``SseSubscriptionRegistry`` 时传入 ``soft_cap``；达到上限直接
  429 + ``Retry-After: 5``，由客户端退避重连，不让运行时无限承压。
- ``heartbeat_ms`` 由客户端按需声明，框架仅夹在 [5000, 60000] 之间；超出周期
  仍无事件就送一条 SSE comment 心跳。
"""

from __future__ import annotations

import asyncio
import json
import logging
from collections import deque
from dataclasses import asdict, is_dataclass
from datetime import datetime, timezone
from typing import Any, AsyncIterator, Awaitable, Callable, Iterable

from fastapi import APIRouter, Depends, HTTPException, Query, Request
from fastapi.responses import StreamingResponse

from polymarket_trader.api.deps import get_runtime
from polymarket_trader.serialization import jsonable

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/stream", tags=["stream"])

HEARTBEAT_MIN_MS = 5_000
HEARTBEAT_MAX_MS = 60_000
HEARTBEAT_DEFAULT_MS = 15_000
LISTENER_QUEUE_MAXSIZE = 1_000


def _coerce_str(value: Any) -> str | None:
    if value is None:
        return None
    text = str(value).strip()
    return text or None


def _event_payload(event: Any) -> dict[str, Any]:
    """Best-effort 把 publish 的事件对象转成 JSON 友好字典。

    旁路设计：序列化只在 SSE consumer 协程里做，绝不阻塞 publish 热路径。
    publish 端只做引用入队。
    """

    if isinstance(event, dict):
        return {str(key): jsonable(item) for key, item in event.items()}
    if is_dataclass(event):
        return jsonable(asdict(event))
    if hasattr(event, "as_dict") and callable(event.as_dict):
        try:
            value = event.as_dict()
        except Exception:
            value = None
        if isinstance(value, dict):
            return {str(key): jsonable(item) for key, item in value.items()}
    # 兜底：暴露最关键的几个字段，避免 SSE 客户端拿不到任何上下文。
    return {
        "event_type": _coerce_str(getattr(event, "event_type", None)) or "unknown",
        "event_id": _coerce_str(getattr(event, "event_id", None)),
        "trace_id": _coerce_str(getattr(event, "trace_id", None)),
        "repr": str(event),
    }


def _event_matches(
    event: Any,
    *,
    event_types: frozenset[str] | None,
    condition_id: str | None,
) -> bool:
    if event_types is not None:
        event_type = _coerce_str(getattr(event, "event_type", None)) or ""
        if event_type not in event_types:
            return False
    if condition_id is not None:
        candidate = _coerce_str(getattr(event, "condition_id", None))
        if candidate != condition_id:
            return False
    return True


class _Listener:
    __slots__ = ("queue", "event_types", "condition_id", "lag_pending", "dropped")

    def __init__(
        self,
        *,
        event_types: frozenset[str] | None,
        condition_id: str | None,
    ) -> None:
        self.queue: asyncio.Queue[Any] = asyncio.Queue(maxsize=LISTENER_QUEUE_MAXSIZE)
        self.event_types = event_types
        self.condition_id = condition_id
        # 单批 lag 只发一条 marker，避免 lag marker 自己撑爆队列。
        self.lag_pending = False
        self.dropped = 0


class _LagMarker:
    """订阅端的特殊事件，标记本 listener 队列发生过 drop-oldest。"""

    __slots__ = ("dropped",)

    def __init__(self, dropped: int) -> None:
        self.dropped = dropped


class SseSubscriptionRegistry:
    """管理 SSE 订阅者集合，并把 EventBus 事件 fan-out 到各 listener 队列。

    ``broadcast`` 是同步回调，注册到 EventBus.add_broadcast_listener；
    实现里只做 ``put_nowait`` + 偶发的 drop-oldest，绝不 await。
    """

    def __init__(self, *, soft_cap: int = 32) -> None:
        self._listeners: set[_Listener] = set()
        self._soft_cap = max(1, soft_cap)
        self._dropped_total = 0

    @property
    def soft_cap(self) -> int:
        return self._soft_cap

    def active_count(self) -> int:
        return len(self._listeners)

    def dropped_events_total(self) -> int:
        return self._dropped_total

    def snapshot(self) -> dict[str, int]:
        return {
            "sse_active_subscribers": self.active_count(),
            "sse_dropped_events_total": self.dropped_events_total(),
            "sse_subscriber_cap": self._soft_cap,
        }

    def register(
        self,
        *,
        event_types: Iterable[str] | None,
        condition_id: str | None,
    ) -> _Listener:
        normalized_types: frozenset[str] | None = None
        if event_types is not None:
            normalized = {item.strip() for item in event_types if item and item.strip()}
            normalized_types = frozenset(normalized) if normalized else None
        listener = _Listener(
            event_types=normalized_types,
            condition_id=_coerce_str(condition_id),
        )
        self._listeners.add(listener)
        return listener

    def unregister(self, listener: _Listener) -> None:
        self._listeners.discard(listener)

    def broadcast(self, event: Any) -> None:
        if not self._listeners:
            return
        # 使用快照迭代，避免 listener 在迭代期间被并发增删导致 RuntimeError。
        for listener in tuple(self._listeners):
            if not _event_matches(
                event,
                event_types=listener.event_types,
                condition_id=listener.condition_id,
            ):
                continue
            try:
                listener.queue.put_nowait(event)
                continue
            except asyncio.QueueFull:
                pass
            # drop-oldest：丢一个最旧事件腾位置，再尝试入队当前事件。
            dropped = self._drop_oldest(listener)
            listener.dropped += dropped
            self._dropped_total += dropped
            if not listener.lag_pending:
                listener.lag_pending = True
                lag = _LagMarker(dropped=listener.dropped)
                try:
                    listener.queue.put_nowait(lag)
                except asyncio.QueueFull:
                    # 极端情况：lag marker 仍塞不进去，留待下次清空后再尝试。
                    listener.lag_pending = False
            try:
                listener.queue.put_nowait(event)
            except asyncio.QueueFull:
                # 本事件直接放弃；订阅端会通过 subscription_lag 知道存在跳过。
                self._dropped_total += 1
                listener.dropped += 1

    @staticmethod
    def _drop_oldest(listener: _Listener) -> int:
        try:
            listener.queue.get_nowait()
            return 1
        except asyncio.QueueEmpty:
            return 0


def _format_sse_data(payload: dict[str, Any]) -> str:
    body = json.dumps(payload, ensure_ascii=False, separators=(",", ":"))
    return f"data: {body}\n\n"


def _format_sse_event(event_type: str, payload: dict[str, Any]) -> str:
    body = json.dumps(payload, ensure_ascii=False, separators=(",", ":"))
    return f"event: {event_type}\ndata: {body}\n\n"


def _heartbeat_frame() -> str:
    # SSE comment 帧（以 ":" 开头），客户端通常用它做 keep-alive。
    return f": heartbeat {datetime.now(timezone.utc).isoformat()}\n\n"


def _utc_iso(value: datetime | None = None) -> str:
    moment = value if value is not None else datetime.now(timezone.utc)
    if moment.tzinfo is None:
        moment = moment.replace(tzinfo=timezone.utc)
    return moment.astimezone(timezone.utc).isoformat()


async def _never_disconnected() -> bool:
    return False


async def iter_listener_frames(
    listener: "_Listener",
    *,
    heartbeat_seconds: float,
    is_disconnected: Callable[[], Awaitable[bool]] = _never_disconnected,
    include_ready: bool = True,
) -> AsyncIterator[bytes]:
    """SSE 帧生成器（可独立于 HTTP 测试）。

    抽出来的目的有两个：让 ``stream_events`` 路由保持瘦身；让单测能直接驱动
    生成器，绕过 httpx ASGITransport 在流式响应上的已知限制。
    """

    try:
        if include_ready:
            ready_payload = {
                "kind": "ready",
                "filters": {
                    "event_types": sorted(listener.event_types) if listener.event_types else None,
                    "condition_id": listener.condition_id,
                },
                "heartbeat_ms": int(heartbeat_seconds * 1000),
                "sent_at": _utc_iso(),
            }
            yield _format_sse_event("ready", ready_payload).encode("utf-8")
        while True:
            if await is_disconnected():
                return
            try:
                item = await asyncio.wait_for(
                    listener.queue.get(),
                    timeout=heartbeat_seconds,
                )
            except asyncio.TimeoutError:
                yield _heartbeat_frame().encode("utf-8")
                continue
            if isinstance(item, _LagMarker):
                listener.lag_pending = False
                lag_payload = {
                    "kind": "subscription_lag",
                    "dropped": item.dropped,
                    "sent_at": _utc_iso(),
                }
                yield _format_sse_event("subscription_lag", lag_payload).encode("utf-8")
                continue
            payload = _event_payload(item)
            yield _format_sse_data(payload).encode("utf-8")
    except asyncio.CancelledError:
        raise


def _get_registry(runtime: Any) -> SseSubscriptionRegistry:
    # R18: runtime.sse_subscription_registry 是 Optional (RuntimeComponents 中默认 None)，
    # 保留 None check 但走属性访问而非 getattr，让 IDE 能查重命名。
    registry = runtime.sse_subscription_registry if runtime is not None else None
    if not isinstance(registry, SseSubscriptionRegistry):
        raise HTTPException(status_code=503, detail="sse_registry_unavailable")
    return registry


@router.get("/events")
async def stream_events(
    request: Request,
    event_types: list[str] | None = Query(default=None, alias="event_types"),
    condition_id: str | None = Query(default=None),
    heartbeat_ms: int = Query(
        default=HEARTBEAT_DEFAULT_MS,
        ge=HEARTBEAT_MIN_MS,
        le=HEARTBEAT_MAX_MS,
    ),
    runtime: Any = Depends(get_runtime),
) -> StreamingResponse:
    registry = _get_registry(runtime)
    if registry.active_count() >= registry.soft_cap:
        raise HTTPException(
            status_code=429,
            detail="sse_subscriber_cap_exceeded",
            headers={"Retry-After": "5"},
        )

    # event_types=foo,bar 与 ?event_types=foo&event_types=bar 都支持。
    requested_types: list[str] | None = None
    if event_types:
        flattened: list[str] = []
        for value in event_types:
            flattened.extend(part for part in value.split(",") if part)
        requested_types = flattened or None

    listener = registry.register(
        event_types=requested_types,
        condition_id=condition_id,
    )
    heartbeat_seconds = max(HEARTBEAT_MIN_MS, min(heartbeat_ms, HEARTBEAT_MAX_MS)) / 1000.0

    async def _stream() -> AsyncIterator[bytes]:
        try:
            async for frame in iter_listener_frames(
                listener,
                heartbeat_seconds=heartbeat_seconds,
                is_disconnected=request.is_disconnected,
            ):
                yield frame
        finally:
            registry.unregister(listener)
            # 主动释放队列引用，避免 GC 滞后导致内存累积。
            drained: deque[Any] = deque()
            while True:
                try:
                    drained.append(listener.queue.get_nowait())
                except asyncio.QueueEmpty:
                    break
            drained.clear()

    return StreamingResponse(
        _stream(),
        media_type="text/event-stream",
        headers={
            "Cache-Control": "no-cache, no-transform",
            "X-Accel-Buffering": "no",
        },
    )
