from __future__ import annotations

from collections.abc import Awaitable, Callable, Mapping
from dataclasses import dataclass, field
from datetime import datetime
from enum import StrEnum
from typing import Any, Protocol


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
