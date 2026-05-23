"""``_publish_allocation_decision`` emit guard 行为。

orderbook 每次更新都会跑 build_entry_plan；只有当 plan 真的产生了候选时
才发 ALLOCATION_DECISION_RECORDED 事件——否则会把 audit 表撑爆。
"""

from __future__ import annotations

import asyncio
from dataclasses import dataclass
from decimal import Decimal
from typing import Any

from polymarket_trader.app.entry_plan import EntryPlan
from polymarket_trader.domain.allocation import AllocationPlan
from polymarket_trader.domain.events import DomainEvent, DomainEventType


@dataclass
class _SpyEventBus:
    published: list[Any]

    async def publish(self, priority: Any, event: Any) -> None:
        self.published.append(event)


def _make_event() -> DomainEvent:
    return DomainEvent(
        trace_id="trace-1",
        event_type=DomainEventType.ORDERBOOK_SNAPSHOT_UPDATED,
        event_id="ev-1",
        condition_id="c1",
        token_id="t1",
    )


def _make_worker(bus: _SpyEventBus) -> Any:
    """构造一个最小的 worker 实例：只为调 _publish_allocation_decision。

    避开 TradingDecisionWorker 全栈装配——本测试只验证 emit guard，
    不需要 trading_decision_service / account_state_store / 等真实依赖。
    """

    from polymarket_trader.workers.trading_decision.worker import TradingDecisionWorker

    class _NoopTradingDecisionService:
        @property
        def strategy_id(self) -> str:
            return "sports_tail"

    worker = TradingDecisionWorker.__new__(TradingDecisionWorker)
    worker._event_bus = bus
    # dedup cache + counter 在 __init__ 里建；__new__ 测试旁路必须显式补齐。
    from collections import OrderedDict

    worker._last_allocation_state_hash = OrderedDict()
    worker._suppressed_allocation_emits = 0
    return worker


def test_no_publish_when_allocation_plan_is_none() -> None:
    bus = _SpyEventBus(published=[])
    worker = _make_worker(bus)
    from polymarket_trader.domain.allocation import AllocationPlan as _AP

    plan = EntryPlan(
        trace_id="trace-1",
        market=None,
        orderbook=None,
        allocation_plan=_AP(trace_id="trace-1", total_budget_usdc=Decimal("0"), allocations=()),
        allocation=None,
        intent=None,
    )
    # 注：_publish_allocation_decision 只对 allocation_plan=None 早返；这里
    # 用空 AllocationPlan 也能验证 emit guard 拦住"无候选"的情况。
    asyncio.run(worker._publish_allocation_decision(event=_make_event(), plan=plan))
    assert bus.published == []


def test_no_publish_when_allocations_empty() -> None:
    bus = _SpyEventBus(published=[])
    worker = _make_worker(bus)
    empty_allocation_plan = AllocationPlan(
        trace_id="trace-1",
        total_budget_usdc=Decimal("0"),
        allocations=(),
    )
    plan = EntryPlan(
        trace_id="trace-1",
        market=None,
        orderbook=None,
        allocation_plan=empty_allocation_plan,
        allocation=None,
        intent=None,
    )
    asyncio.run(worker._publish_allocation_decision(event=_make_event(), plan=plan))
    assert bus.published == []


def test_publish_when_allocations_non_empty() -> None:
    from polymarket_trader.domain.allocation import Allocation

    bus = _SpyEventBus(published=[])
    worker = _make_worker(bus)
    allocation_plan = AllocationPlan(
        trace_id="trace-1",
        total_budget_usdc=Decimal("100"),
        allocations=(
            Allocation(
                strategy_id="sports_tail",
                condition_id="c1",
                token_id="t1",
                target_budget_usdc=Decimal("10"),
                buy_budget_usdc=Decimal("10"),
                market_slug="m1",
                current_exposure_usdc=Decimal("0"),
                released_budget_usdc=Decimal("0"),
                reason="",
                idempotency_key="idem-1",
            ),
        ),
    )
    plan = EntryPlan(
        trace_id="trace-1",
        market=None,
        orderbook=None,
        allocation_plan=allocation_plan,
        allocation=None,
        intent=None,
    )
    asyncio.run(worker._publish_allocation_decision(event=_make_event(), plan=plan))
    assert len(bus.published) == 1
    event = bus.published[0]
    assert event.event_type.value == "allocation_decision_recorded"
    assert event.payload["candidates"][0]["condition_id"] == "c1"
