"""framework 内部 lifecycle 总线实现。

策略通过 ``RuntimePorts.lifecycle`` 拿到的是 ``LifecycleBus`` Protocol 的只读视图；
本模块的 ``InProcessLifecycleBus`` 同时暴露 ``publish`` 给 framework 自己用，
publish 接口被抽象为 ``LifecyclePublisher`` Protocol，让 framework 内部依赖这个
Protocol 而不是具体实现，将来换 in-process / cross-process 实现都不破调用面。

回调执行隔离：每个回调用 ``asyncio.create_task`` 包起来，抛错被 framework 捕获、
转 telemetry，不阻塞 publish 调用方（主链路 worker / runtime）。
"""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Awaitable, Callable, Mapping
from dataclasses import dataclass, field
from datetime import datetime, timezone
from enum import StrEnum
from itertools import count
from typing import Any, Protocol


# ===== from former contracts/lifecycle.py =====
class LifecycleEvent(StrEnum):
    """framework 暴露给策略订阅的生命周期事件。

    策略不能 publish。framework 在主链路已有事件发出点增量发布到 lifecycle bus，
    避免破坏 P0 队列分配。回调异步执行、抛错只走 telemetry，不影响主链路。

    每个事件的 envelope.payload 字段：

    - ORDER_SUBMITTED / ORDER_FILLED / ORDER_REJECTED / ORDER_CANCELLED:
      ``{operation, status, order_id, trade_id, matched_shares, spent_usdc, reason, retryable, intent_tags}``
    - LIVE_STATE_UPDATED:
      ``{source, source_event_id, signal_allowed, signal_reason, phase, payload}``
    - LIVE_STATE_SOURCE_EVICTED:
      ``{source, last_error, consecutive_failures, cooldown_until}``
    - LIVE_STATE_NO_FEASIBLE_SOURCE:
      ``{observed_at, source_statuses}``
    - SEASON_STATE_UPDATED:
      ``{league, season_id, observed_at, payload}``
    - RECONCILE_PASSED:
      ``{trace_id, condition_ids_count, started_at, completed_at, outcome}``
    """

    ORDER_SUBMITTED = "order_submitted"
    ORDER_FILLED = "order_filled"
    ORDER_REJECTED = "order_rejected"
    ORDER_CANCELLED = "order_cancelled"
    LIVE_STATE_UPDATED = "live_state_updated"
    LIVE_STATE_SOURCE_EVICTED = "live_state_source_evicted"
    LIVE_STATE_NO_FEASIBLE_SOURCE = "live_state_no_feasible_source"
    SEASON_STATE_UPDATED = "season_state_updated"
    RECONCILE_PASSED = "reconcile_passed"


@dataclass(frozen=True, slots=True)
class LifecycleEnvelope:
    """单条生命周期通知。"""

    event: LifecycleEvent
    occurred_at: datetime
    trace_id: str | None = None
    condition_id: str | None = None
    token_id: str | None = None
    market_slug: str | None = None
    payload: Mapping[str, Any] = field(default_factory=dict)


@dataclass(frozen=True, slots=True)
class SubscriptionHandle:
    """订阅 handle；策略持有以便 ``unsubscribe``。"""

    subscription_id: int


LifecycleCallback = Callable[[LifecycleEnvelope], Awaitable[None]]


class LifecycleBus(Protocol):
    """策略订阅 framework lifecycle 事件的只读总线。

    策略只能 ``subscribe`` / ``unsubscribe``——publish 是 framework 内部接口。
    """

    def subscribe(self, event: LifecycleEvent, callback: LifecycleCallback) -> SubscriptionHandle: ...

    def unsubscribe(self, handle: SubscriptionHandle) -> None: ...



class LifecyclePublisher(Protocol):
    """framework 内部 publisher 视图：只暴露 publish。"""

    def publish(
        self,
        event: LifecycleEvent,
        *,
        trace_id: str | None = None,
        condition_id: str | None = None,
        token_id: str | None = None,
        market_slug: str | None = None,
        payload: Mapping[str, Any] | None = None,
        occurred_at: datetime | None = None,
    ) -> None: ...

_logger = logging.getLogger(__name__)


def _utc_now() -> datetime:
    return datetime.now(timezone.utc)


class InProcessLifecycleBus(LifecycleBus):
    """单进程 lifecycle 总线。"""

    def __init__(self) -> None:
        self._subscribers: dict[LifecycleEvent, dict[int, LifecycleCallback]] = {}
        self._subscription_index: dict[int, LifecycleEvent] = {}
        self._sequence = count(1)

    def subscribe(self, event: LifecycleEvent, callback: LifecycleCallback) -> SubscriptionHandle:
        subscription_id = next(self._sequence)
        self._subscribers.setdefault(event, {})[subscription_id] = callback
        self._subscription_index[subscription_id] = event
        return SubscriptionHandle(subscription_id=subscription_id)

    def unsubscribe(self, handle: SubscriptionHandle) -> None:
        event = self._subscription_index.pop(handle.subscription_id, None)
        if event is None:
            return
        bucket = self._subscribers.get(event)
        if bucket is not None:
            bucket.pop(handle.subscription_id, None)
            if not bucket:
                self._subscribers.pop(event, None)

    def publish(
        self,
        event: LifecycleEvent,
        *,
        trace_id: str | None = None,
        condition_id: str | None = None,
        token_id: str | None = None,
        market_slug: str | None = None,
        payload: Mapping[str, Any] | None = None,
        occurred_at: datetime | None = None,
    ) -> None:
        """framework 内部调用。同步入队 + 异步分发；调用方不感知回调耗时。"""

        bucket = self._subscribers.get(event)
        if not bucket:
            return
        envelope = LifecycleEnvelope(
            event=event,
            occurred_at=occurred_at or _utc_now(),
            trace_id=trace_id,
            condition_id=condition_id,
            token_id=token_id,
            market_slug=market_slug,
            payload=dict(payload or {}),
        )
        try:
            loop = asyncio.get_running_loop()
        except RuntimeError:
            # 同步上下文（如启动期或测试）：直接同步调用，但仍然吞回调异常。
            for callback in tuple(bucket.values()):
                self._invoke_sync(callback, envelope)
            return
        for callback in tuple(bucket.values()):
            loop.create_task(self._invoke_async(callback, envelope))

    async def _invoke_async(self, callback: LifecycleCallback, envelope: LifecycleEnvelope) -> None:
        try:
            await callback(envelope)
        except Exception:
            _logger.exception(
                "lifecycle callback raised; event=%s trace_id=%s condition_id=%s",
                envelope.event.value,
                envelope.trace_id,
                envelope.condition_id,
            )

    def _invoke_sync(self, callback: LifecycleCallback, envelope: LifecycleEnvelope) -> None:
        # 仅在 publish 调用方不在 async 上下文（如启动期 / 同步测试）时走到这里。
        # 生产 publish 全部来自 TradingService / SportsLiveStateWorker / ReconcileWorker
        # 的 async 调用栈，永远不会落到这条路径。
        coro = callback(envelope)
        if coro is None:
            return
        try:
            asyncio.run(coro)
        except Exception:
            _logger.exception(
                "lifecycle callback raised in sync context; event=%s",
                envelope.event.value,
            )
