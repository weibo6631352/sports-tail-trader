"""``_publish_allocation_decision`` 高频事件 dedup 行为。

orderbook 每 tick 都跑分配决策；同一市场 candidate / reason / 是否拿到 buy_budget
没变时跳过 publish，避免 audit_events 一天千万条。
"""

from __future__ import annotations

import asyncio
from collections import OrderedDict
from dataclasses import dataclass
from decimal import Decimal
from typing import Any

from polymarket_trader.app.entry_plan import EntryPlan
from polymarket_trader.domain.allocation import Allocation, AllocationPlan
from polymarket_trader.domain.events import DomainEvent, DomainEventType
from polymarket_trader.workers.trading_decision.worker import (
    TradingDecisionWorker,
    _ALLOCATION_DEDUPE_CAPACITY,
)


@dataclass
class _SpyEventBus:
    published: list[Any]

    async def publish(self, priority: Any, event: Any) -> None:
        self.published.append(event)


def _make_event(condition_id: str = "c1", token_id: str = "t1") -> DomainEvent:
    return DomainEvent(
        trace_id="trace-1",
        event_type=DomainEventType.ORDERBOOK_SNAPSHOT_UPDATED,
        event_id="ev-1",
        condition_id=condition_id,
        token_id=token_id,
    )


def _make_worker(bus: _SpyEventBus) -> TradingDecisionWorker:
    worker = TradingDecisionWorker.__new__(TradingDecisionWorker)
    worker._event_bus = bus
    worker._last_allocation_state_hash = OrderedDict()
    worker._suppressed_allocation_emits = 0
    return worker


def _make_plan(
    *,
    condition_id: str = "c1",
    token_id: str = "t1",
    buy_budget: Decimal = Decimal("10"),
    target_budget: Decimal = Decimal("10"),
    reason: str = "",
    release_reason: str = "",
    plan_reason: str = "",
) -> EntryPlan:
    allocation_plan = AllocationPlan(
        trace_id="trace-1",
        total_budget_usdc=Decimal("100"),
        allocations=(
            Allocation(
                strategy_id="sports_tail",
                condition_id=condition_id,
                token_id=token_id,
                target_budget_usdc=target_budget,
                buy_budget_usdc=buy_budget,
                market_slug="m1",
                current_exposure_usdc=Decimal("0"),
                released_budget_usdc=Decimal("0"),
                reason=reason,
                release_reason=release_reason,
                idempotency_key="idem-1",
            ),
        ),
        reason=plan_reason,
    )
    return EntryPlan(
        trace_id="trace-1",
        market=None,
        orderbook=None,
        allocation_plan=allocation_plan,
        allocation=None,
        intent=None,
    )


def test_duplicate_plan_suppressed() -> None:
    bus = _SpyEventBus(published=[])
    worker = _make_worker(bus)
    plan = _make_plan()
    asyncio.run(worker._publish_allocation_decision(event=_make_event(), plan=plan))
    asyncio.run(worker._publish_allocation_decision(event=_make_event(), plan=plan))
    assert len(bus.published) == 1
    assert worker.suppressed_allocation_emits == 1


def test_reason_change_reemits() -> None:
    bus = _SpyEventBus(published=[])
    worker = _make_worker(bus)
    plan1 = _make_plan(reason="a")
    plan2 = _make_plan(reason="b")
    asyncio.run(worker._publish_allocation_decision(event=_make_event(), plan=plan1))
    asyncio.run(worker._publish_allocation_decision(event=_make_event(), plan=plan2))
    assert len(bus.published) == 2
    assert worker.suppressed_allocation_emits == 0


def test_budget_only_change_suppressed() -> None:
    """精确 buy_budget 抖动（保持 >0）不应触发重发——hash 只看是否拿到预算的布尔位。"""
    bus = _SpyEventBus(published=[])
    worker = _make_worker(bus)
    plan1 = _make_plan(buy_budget=Decimal("10"))
    plan2 = _make_plan(buy_budget=Decimal("11.5"))
    asyncio.run(worker._publish_allocation_decision(event=_make_event(), plan=plan1))
    asyncio.run(worker._publish_allocation_decision(event=_make_event(), plan=plan2))
    assert len(bus.published) == 1
    assert worker.suppressed_allocation_emits == 1


def test_budget_zero_transition_reemits() -> None:
    """buy_budget 从 >0 跨到 ==0 是决策本质变化——必须 emit。"""
    bus = _SpyEventBus(published=[])
    worker = _make_worker(bus)
    plan1 = _make_plan(buy_budget=Decimal("10"))
    plan2 = _make_plan(buy_budget=Decimal("0"))
    asyncio.run(worker._publish_allocation_decision(event=_make_event(), plan=plan1))
    asyncio.run(worker._publish_allocation_decision(event=_make_event(), plan=plan2))
    assert len(bus.published) == 2


def test_lru_evicts_oldest_when_capacity_exceeded() -> None:
    bus = _SpyEventBus(published=[])
    worker = _make_worker(bus)
    # 灌满 + 1
    for i in range(_ALLOCATION_DEDUPE_CAPACITY + 1):
        cid = f"c{i}"
        event = _make_event(condition_id=cid, token_id=f"t{i}")
        plan = _make_plan(condition_id=cid, token_id=f"t{i}")
        asyncio.run(worker._publish_allocation_decision(event=event, plan=plan))
    assert len(worker._last_allocation_state_hash) == _ALLOCATION_DEDUPE_CAPACITY
    # 第一个 key 应被淘汰
    assert ("c0", "t0") not in worker._last_allocation_state_hash
    # 最新的 key 仍在
    assert (f"c{_ALLOCATION_DEDUPE_CAPACITY}", f"t{_ALLOCATION_DEDUPE_CAPACITY}") in worker._last_allocation_state_hash
