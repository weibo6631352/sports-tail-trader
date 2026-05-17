from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass
from enum import IntEnum
from itertools import count
from typing import Any, Callable

from polymarket_trader.domain.events import DomainEventType, OutboxPriority

logger = logging.getLogger(__name__)
# 防 dict / queue drift 的健康度告警阈值——dict size > queue capacity × 此倍数 触发 warning。
# 1.5 给瞬时抖动留余地（merge 期间临时多一两条），同时能在真有 race / leak 时发声。
_TRADING_PENDING_DICT_WARN_MULTIPLIER = 1.5


# 这里不另起一套同义枚举，直接复用域内优先级定义，避免队列和 outbox 之间出现两套语义。
EventPriority = OutboxPriority


class QueueLane(IntEnum):
    TRADING = 0
    MAINTENANCE = 2
    PERSISTENCE = 3


@dataclass(frozen=True, slots=True)
class QueueDepthSnapshot:
    trading_queue_depth: int
    maintenance_queue_depth: int
    persistence_queue_depth: int
    trading_queue_capacity: int
    maintenance_queue_capacity: int
    persistence_queue_capacity: int
    trading_retained_depth: int
    maintenance_retained_depth: int
    persistence_retained_depth: int
    low_priority_paused: bool


def _normalize_priority(priority: EventPriority | int | str) -> QueueLane:
    if isinstance(priority, EventPriority):
        if priority in {OutboxPriority.P0, OutboxPriority.P1}:
            return QueueLane.TRADING
        if priority == OutboxPriority.P2:
            return QueueLane.MAINTENANCE
        return QueueLane.PERSISTENCE
    if isinstance(priority, str):
        text = priority.strip().upper()
        if text.startswith("P") and text[1:].isdigit():
            return _normalize_priority(int(text[1:]))
        return _normalize_priority(int(text))
    if int(priority) <= 1:
        return QueueLane.TRADING
    if int(priority) == 2:
        return QueueLane.MAINTENANCE
    return QueueLane.PERSISTENCE


def _event_identity(event: Any) -> str:
    for attr in ("merge_key", "dedupe_key", "idempotency_key"):
        value = getattr(event, attr, None)
        if value:
            return str(value)
    for attr in ("event_id", "trace_id"):
        value = getattr(event, attr, None)
        if value:
            return str(value)
    return f"event:{id(event)}"


@dataclass(frozen=True, slots=True)
class _RetainedEvent:
    key: str
    event: Any


class EventBus:
    def __init__(
        self,
        max_size: int | None = None,
        *,
        trading_capacity: int | None = None,
        maintenance_capacity: int | None = None,
        persistence_capacity: int | None = None,
        retained_capacity: int | None = None,
    ) -> None:
        if max_size is not None:
            trading_capacity = trading_capacity or max_size
            maintenance_capacity = maintenance_capacity or max_size
            persistence_capacity = persistence_capacity or max_size

        self._trading_capacity = trading_capacity or 1000
        self._maintenance_capacity = maintenance_capacity or 1000
        self._persistence_capacity = persistence_capacity or 5000
        self._retained_capacity = retained_capacity or max(
            self._trading_capacity,
            self._maintenance_capacity,
            self._persistence_capacity,
        )
        self._sequence = count()
        self._trading_queue: asyncio.Queue[Any] = asyncio.Queue(maxsize=self._trading_capacity)
        self._maintenance_queue: asyncio.Queue[Any] = asyncio.Queue(
            maxsize=self._maintenance_capacity,
        )
        self._persistence_queue: asyncio.Queue[Any] = asyncio.Queue(
            maxsize=self._persistence_capacity,
        )
        self._trading_pending_events: dict[str, Any] = {}
        self._retained: dict[QueueLane, list[_RetainedEvent]] = {
            QueueLane.MAINTENANCE: [],
            QueueLane.PERSISTENCE: [],
        }
        self._low_priority_paused = False
        self._wake = asyncio.Event()
        self._persistence_sink: Callable[[int, Any], None] | None = None
        # 只读旁路：SSE / admin 订阅可以挂这里，单纯做 fan-out，不影响交易主链路。
        self._broadcast_callbacks: set[Callable[[Any], None]] = set()
        # outbox mirror 失败计数器——§10 要求降级动作可审计。失败仍然不向上抛
        # 异常以保 §7 主链路不被反向阻塞，但累计计数 + 警告日志能让 observability
        # 看到 sink 故障。
        self._mirror_failure_count = 0

    def bind_persistence_sink(self, sink: Callable[[int, Any], None] | None) -> None:
        self._persistence_sink = sink

    def add_broadcast_listener(self, callback: Callable[[Any], None]) -> None:
        """注册只读 fan-out 回调（SSE 等订阅入口用）。"""

        self._broadcast_callbacks.add(callback)

    def remove_broadcast_listener(self, callback: Callable[[Any], None]) -> None:
        self._broadcast_callbacks.discard(callback)

    def publish_nowait(self, priority: EventPriority | int | str, event: Any) -> None:
        """P0 热路径专用的同步 fire-and-forget 投递。

        与 ``publish`` 行为对齐，但不返回协程；P0/P1 队列满时不 await，转而走 retain
        + mirror，保证 §7 主链路绝不被 EventBus 反向阻塞。
        """

        outbox_priority = _normalize_outbox_priority(priority)
        lane = _normalize_priority(priority)
        if lane == QueueLane.PERSISTENCE and self._persistence_sink is not None:
            self._mirror_to_outbox(outbox_priority, event)
            self._broadcast(event)
            return
        if lane == QueueLane.TRADING:
            key = self._trading_event_key(event)
            if key in self._trading_pending_events:
                self._trading_pending_events[key] = event
                self._mirror_to_outbox(outbox_priority, event)
                self._broadcast(event)
                self._wake.set()
                return
            try:
                self._trading_queue.put_nowait((next(self._sequence), key))
                self._trading_pending_events[key] = event
            except asyncio.QueueFull:
                # 主链路绝不能 await；P0 满了仍要保证 outbox 拿到拒绝事件以便审计。
                self._mirror_to_outbox(outbox_priority, event)
                self._broadcast(event)
                logger.warning(
                    "event_bus: trading queue full on publish_nowait; mirrored to outbox only",
                    extra={"event_type": str(getattr(event, "event_type", ""))},
                )
                return
            self._mirror_to_outbox(outbox_priority, event)
            self._broadcast(event)
            self._wake.set()
            return
        if self._low_priority_paused:
            self._retain(lane, event)
            self._mirror_to_outbox(outbox_priority, event)
            self._broadcast(event)
            self._wake.set()
            return
        queue = self._queue_for_lane(lane)
        try:
            queue.put_nowait((next(self._sequence), event))
        except asyncio.QueueFull:
            self._retain(lane, event)
        self._mirror_to_outbox(outbox_priority, event)
        self._broadcast(event)
        self._wake.set()

    async def publish(self, priority: EventPriority | int | str, event: Any) -> None:
        outbox_priority = _normalize_outbox_priority(priority)
        lane = _normalize_priority(priority)
        if lane == QueueLane.PERSISTENCE and self._persistence_sink is not None:
            # P3 只需要镜像到 outbox；运行时没有独立的 persistence lane consumer，
            # 继续把这类事件塞进队列只会制造永远不会被消费的积压。
            self._mirror_to_outbox(outbox_priority, event)
            self._broadcast(event)
            return
        if lane == QueueLane.TRADING:
            key = self._trading_event_key(event)
            if key in self._trading_pending_events:
                self._trading_pending_events[key] = event
                self._mirror_to_outbox(outbox_priority, event)
                self._broadcast(event)
                self._wake.set()
                return
            await self._trading_queue.put((next(self._sequence), key))
            self._trading_pending_events[key] = event
            self._mirror_to_outbox(outbox_priority, event)
            self._broadcast(event)
            self._wake.set()
            # health-check：pending dict 应当 ≤ queue depth + 少量正在 merge 的 key。
            # 显著超出意味着消费侧泄漏（pop 漏掉）或并发改写丢失。
            pending_warn_cap = int(self._trading_capacity * _TRADING_PENDING_DICT_WARN_MULTIPLIER)
            if len(self._trading_pending_events) > pending_warn_cap:
                logger.warning(
                    "event_bus: trading_pending_events drift detected size=%d > %d (queue_cap=%d). "
                    "Potential consumer leak or merge race.",
                    len(self._trading_pending_events),
                    pending_warn_cap,
                    self._trading_capacity,
                )
            return

        # P2 / P3 不能反向堵住 P0，所以低优先级只能尽量入队，满了就先缓存在本地。
        if self._low_priority_paused:
            self._retain(lane, event)
            self._mirror_to_outbox(outbox_priority, event)
            self._broadcast(event)
            self._wake.set()
            return

        queue = self._queue_for_lane(lane)
        try:
            queue.put_nowait((next(self._sequence), event))
            self._mirror_to_outbox(outbox_priority, event)
            self._broadcast(event)
            self._wake.set()
        except asyncio.QueueFull:
            self._retain(lane, event)
            self._mirror_to_outbox(outbox_priority, event)
            self._broadcast(event)
            self._wake.set()

    async def next_event(self) -> Any:
        while True:
            await self._flush_retained_best_effort()
            event = self._pop_next_ready()
            if event is not None:
                return event
            self._wake.clear()
            await self._wake.wait()

    async def next_trading_event(self) -> Any:
        while True:
            _, key = await self._trading_queue.get()
            event = self._trading_pending_events.pop(key, None)
            if event is None:
                continue
            await self._flush_retained_best_effort()
            return event

    async def next_maintenance_event(self) -> Any:
        _, event = await self._maintenance_queue.get()
        await self._flush_retained_best_effort()
        return event

    async def next_persistence_event(self) -> Any:
        _, event = await self._persistence_queue.get()
        await self._flush_retained_best_effort()
        return event

    def pause_low_priority(self) -> None:
        self._low_priority_paused = True

    async def resume_low_priority(self) -> None:
        self._low_priority_paused = False
        await self._flush_retained_best_effort()
        self._wake.set()

    def snapshot(self) -> QueueDepthSnapshot:
        return QueueDepthSnapshot(
            trading_queue_depth=self.trading_queue_depth(),
            maintenance_queue_depth=self._maintenance_queue.qsize(),
            persistence_queue_depth=self._persistence_queue.qsize(),
            trading_queue_capacity=self._trading_capacity,
            maintenance_queue_capacity=self._maintenance_capacity,
            persistence_queue_capacity=self._persistence_capacity,
            trading_retained_depth=0,
            maintenance_retained_depth=len(self._retained[QueueLane.MAINTENANCE]),
            persistence_retained_depth=len(self._retained[QueueLane.PERSISTENCE]),
            low_priority_paused=self._low_priority_paused,
        )

    def trading_queue_depth(self) -> int:
        return len(self._trading_pending_events)

    def maintenance_queue_depth(self) -> int:
        return self._maintenance_queue.qsize()

    def persistence_queue_depth(self) -> int:
        return self._persistence_queue.qsize()

    def low_priority_paused(self) -> bool:
        return self._low_priority_paused

    async def _flush_retained_best_effort(self) -> None:
        if self._low_priority_paused:
            return
        for lane in (QueueLane.MAINTENANCE, QueueLane.PERSISTENCE):
            queue = self._queue_for_lane(lane)
            retained = self._retained[lane]
            if not retained:
                continue
            moved: list[_RetainedEvent] = []
            while retained and not queue.full():
                item = retained.pop(0)
                try:
                    queue.put_nowait((next(self._sequence), item.event))
                    moved.append(item)
                except asyncio.QueueFull:
                    retained.insert(0, item)
                    break
            if moved:
                self._wake.set()

    def _retain(self, lane: QueueLane, event: Any) -> None:
        retained = self._retained[lane]
        key = _event_identity(event)
        for index, item in enumerate(retained):
            if item.key == key:
                retained[index] = _RetainedEvent(key=key, event=event)
                return
        if len(retained) >= self._retained_capacity:
            retained.pop(0)
        retained.append(_RetainedEvent(key=key, event=event))

    def _mirror_to_outbox(self, priority: int, event: Any) -> None:
        if self._persistence_sink is None:
            return
        try:
            self._persistence_sink(priority, event)
        except Exception:
            # 不重新抛出：mirror 是审计副作用，不能反向阻塞 publish 主链路。但必须
            # 留痕——只静默吞掉会让 outbox 故障完全不可见（§10 可审计性）。
            self._mirror_failure_count += 1
            logger.warning(
                "event_bus: outbox mirror failed; event dropped from persistence sink",
                exc_info=True,
                extra={
                    "priority": priority,
                    "event_type": str(getattr(event, "event_type", "")),
                    "event_id": str(getattr(event, "event_id", "")),
                    "trace_id": str(getattr(event, "trace_id", "")),
                    "mirror_failure_count": self._mirror_failure_count,
                },
            )

    def mirror_failure_count(self) -> int:
        """累计的 outbox sink 失败次数；供 supervisor / admin 观测。"""

        return self._mirror_failure_count

    def _broadcast(self, event: Any) -> None:
        # 热路径旁路：无订阅者时直接返回（O(1)），保证 publish() 在常态下零额外开销。
        # 任何监听器异常都吞掉并 DEBUG 日志；旁路不允许反向影响交易主链路。
        if not self._broadcast_callbacks:
            return
        for callback in tuple(self._broadcast_callbacks):
            try:
                callback(event)
            except Exception:
                logger.debug("event_bus broadcast listener failed", exc_info=True)

    def _trading_event_key(self, event: Any) -> str:
        merge_key = getattr(event, "merge_key", None)
        if merge_key:
            return str(merge_key)
        event_id = getattr(event, "event_id", None)
        if event_id:
            return str(event_id)
        return f"event:{id(event)}"

    def _queue_for_lane(self, lane: QueueLane) -> asyncio.Queue[Any]:
        if lane == QueueLane.MAINTENANCE:
            return self._maintenance_queue
        if lane == QueueLane.PERSISTENCE:
            return self._persistence_queue
        return self._trading_queue

    def _pop_next_ready(self) -> Any | None:
        # P0 必须先出队，P2 / P3 只能在 trading 为空时被消费，避免维护流反向抢占交易流。
        trading_event = self._pop_trading_event_nowait()
        if trading_event is not None:
            return trading_event
        for queue in (self._maintenance_queue, self._persistence_queue):
            try:
                _, event = queue.get_nowait()
                return event
            except asyncio.QueueEmpty:
                continue
        return None

    def _pop_trading_event_nowait(self) -> Any | None:
        while True:
            try:
                _, key = self._trading_queue.get_nowait()
            except asyncio.QueueEmpty:
                return None
            event = self._trading_pending_events.pop(key, None)
            if event is not None:
                return event


def _normalize_outbox_priority(priority: EventPriority | int | str) -> int:
    if isinstance(priority, EventPriority):
        text = priority.value.strip().upper()
        return int(text[1:]) if text.startswith("P") and text[1:].isdigit() else int(text)
    if isinstance(priority, str):
        text = priority.strip().upper()
        if text.startswith("P") and text[1:].isdigit():
            return int(text[1:])
        return int(text)
    return int(priority)
