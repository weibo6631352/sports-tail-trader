from __future__ import annotations

import asyncio
from decimal import Decimal

from polymarket_trader.app.trading_service import TradingService
from polymarket_trader.domain.order import (
    BuyOrderIntent,
    ExecutionTimestamps,
    OrderResult,
    OrderResultStatus,
    OrderSide,
    OrderType,
)
from polymarket_trader.domain.risk import RiskDecision, RiskManager
from polymarket_trader.extension_api.lifecycle import LifecycleEnvelope, LifecycleEvent
from polymarket_trader.runtime.lifecycle_bus import InProcessLifecycleBus


class _StubExecutor:
    """简化执行器：根据预设 status 返回 OrderResult，不实际下单。"""

    def __init__(self, status: OrderResultStatus) -> None:
        self._status = status

    async def submit(self, intent: BuyOrderIntent) -> OrderResult:
        return OrderResult(
            trace_id=intent.trace_id,
            condition_id=intent.condition_id,
            token_id=intent.token_id,
            status=self._status,
            intent=intent,
            order_id="order-1",
            side=OrderSide.BUY,
            order_type=intent.order_type,
            price=intent.price,
            requested_amount_usdc=intent.amount_usdc,
            matched_shares=Decimal("3") if self._status == OrderResultStatus.FULL_FILL else Decimal("0"),
            spent_usdc=Decimal("3") if self._status == OrderResultStatus.FULL_FILL else Decimal("0"),
            timestamps=ExecutionTimestamps(),
        )


class _AlwaysPassRisk(RiskManager):
    def check_order_intent(self, *args, **kwargs) -> RiskDecision:  # type: ignore[override]
        return RiskDecision(passed=True)


def _build_intent() -> BuyOrderIntent:
    return BuyOrderIntent(
        trace_id="trace-lifecycle",
        condition_id="cond-1",
        token_id="tok-1",
        price=Decimal("0.5"),
        amount_usdc=Decimal("3"),
        order_type=OrderType.FAK,
        market_slug="slug-1",
        intent_tags=frozenset({"scale_in"}),
    )


def test_filled_order_publishes_order_filled() -> None:
    bus = InProcessLifecycleBus()
    received: list[LifecycleEnvelope] = []

    async def callback(envelope: LifecycleEnvelope) -> None:
        received.append(envelope)

    bus.subscribe(LifecycleEvent.ORDER_FILLED, callback)
    service = TradingService(
        risk_manager=_AlwaysPassRisk(),
        executor=_StubExecutor(OrderResultStatus.FULL_FILL),
        lifecycle_bus=bus,
    )

    async def run() -> None:
        await service.review_intent(_build_intent(), operation="buy")
        await asyncio.sleep(0)

    asyncio.run(run())
    assert len(received) == 1
    assert received[0].event == LifecycleEvent.ORDER_FILLED
    assert received[0].condition_id == "cond-1"
    assert received[0].payload["status"] == OrderResultStatus.FULL_FILL.value
    assert received[0].payload["intent_tags"] == ("scale_in",)


def test_risk_rejected_publishes_order_rejected() -> None:
    bus = InProcessLifecycleBus()
    received: list[LifecycleEnvelope] = []

    async def callback(envelope: LifecycleEnvelope) -> None:
        received.append(envelope)

    class _DenyRisk(RiskManager):
        def check_order_intent(self, *args, **kwargs) -> RiskDecision:  # type: ignore[override]
            return RiskDecision(passed=False, reason="budget_exhausted")

    bus.subscribe(LifecycleEvent.ORDER_REJECTED, callback)
    service = TradingService(
        risk_manager=_DenyRisk(),
        executor=_StubExecutor(OrderResultStatus.FULL_FILL),
        lifecycle_bus=bus,
    )

    async def run() -> None:
        await service.review_intent(_build_intent(), operation="buy")
        await asyncio.sleep(0)

    asyncio.run(run())
    assert len(received) == 1
    assert received[0].event == LifecycleEvent.ORDER_REJECTED


def test_no_lifecycle_bus_does_not_break_main_path() -> None:
    service = TradingService(
        risk_manager=_AlwaysPassRisk(),
        executor=_StubExecutor(OrderResultStatus.FULL_FILL),
    )

    async def run() -> None:
        return await service.review_intent(_build_intent(), operation="buy")

    result = asyncio.run(run())
    assert result.order_result is not None
    assert result.order_result.status == OrderResultStatus.FULL_FILL
