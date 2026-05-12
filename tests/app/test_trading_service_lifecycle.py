from __future__ import annotations

import asyncio
from decimal import Decimal

from polymarket_trader.app.trading_service import TradingService
from polymarket_trader.domain.order import (
    BuyOrderIntent,
    CancelOrderIntent,
    ExecutionTimestamps,
    OrderResult,
    OrderResultStatus,
    OrderSide,
    OrderType,
    ReplaceOrderIntent,
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
            strategy_id="sports_tail",
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
        strategy_id="sports_tail",
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


# ============================================================
# 扩展：cancel / replace 控制路径 + 状态映射 + 边界
# ============================================================

class _StubControlExecutor:
    """覆盖 cancel/replace 的 stub；按 method 名记录调用 + 返回预设 OrderResult。"""

    def __init__(self, *, cancel_status: OrderResultStatus | None = None, replace_status: OrderResultStatus | None = None) -> None:
        self._cancel_status = cancel_status
        self._replace_status = replace_status
        self.calls: list[tuple[str, object]] = []

    async def cancel(self, intent: CancelOrderIntent) -> OrderResult:
        self.calls.append(("cancel", intent))
        return _build_result_for(intent, self._cancel_status or OrderResultStatus.CANCELLED)

    async def replace(self, intent: ReplaceOrderIntent) -> OrderResult:
        self.calls.append(("replace", intent))
        return _build_result_for(intent, self._replace_status or OrderResultStatus.FULL_FILL)


def _build_result_for(intent: object, status: OrderResultStatus) -> OrderResult:
    return OrderResult(
        strategy_id="sports_tail",
        trace_id=getattr(intent, "trace_id", "trace-x"),
        condition_id=getattr(intent, "condition_id", "cond-x"),
        token_id=getattr(intent, "token_id", "tok-x"),
        status=status,
        intent=intent,  # type: ignore[arg-type]
        order_id=getattr(intent, "order_id", "order-x"),
        side=OrderSide.BUY,
        order_type=OrderType.GTC,
        price=Decimal("0.5"),
        timestamps=ExecutionTimestamps(),
    )


def _build_cancel_intent() -> CancelOrderIntent:
    return CancelOrderIntent(
        strategy_id="sports_tail",
        trace_id="trace-cancel-1",
        condition_id="cond-1",
        token_id="tok-1",
        order_id="exchange-order-id-9",
        reason="user_requested",
    )


def _build_replace_intent() -> ReplaceOrderIntent:
    return ReplaceOrderIntent(
        strategy_id="sports_tail",
        trace_id="trace-replace-1",
        condition_id="cond-1",
        token_id="tok-1",
        order_id="exchange-order-id-9",
        new_price=Decimal("0.55"),
        size_shares=Decimal("4"),
    )


def _collect(bus: InProcessLifecycleBus, event: LifecycleEvent) -> list[LifecycleEnvelope]:
    received: list[LifecycleEnvelope] = []

    async def callback(envelope: LifecycleEnvelope) -> None:
        received.append(envelope)

    bus.subscribe(event, callback)
    return received


def test_cancel_publishes_order_cancelled() -> None:
    bus = InProcessLifecycleBus()
    received = _collect(bus, LifecycleEvent.ORDER_CANCELLED)
    executor = _StubControlExecutor(cancel_status=OrderResultStatus.CANCELLED)
    service = TradingService(risk_manager=_AlwaysPassRisk(), executor=executor, lifecycle_bus=bus)

    async def run() -> None:
        await service.cancel(_build_cancel_intent())
        await asyncio.sleep(0)

    asyncio.run(run())
    assert [kind for kind, _ in executor.calls] == ["cancel"]
    assert len(received) == 1
    assert received[0].event == LifecycleEvent.ORDER_CANCELLED
    assert received[0].payload["status"] == OrderResultStatus.CANCELLED.value


def test_cancel_failure_publishes_order_rejected() -> None:
    """cancel 路径但 executor 返回 FAILED/REJECTED 状态 → ORDER_REJECTED。"""
    bus = InProcessLifecycleBus()
    received = _collect(bus, LifecycleEvent.ORDER_REJECTED)
    executor = _StubControlExecutor(cancel_status=OrderResultStatus.REJECTED)
    service = TradingService(risk_manager=_AlwaysPassRisk(), executor=executor, lifecycle_bus=bus)

    async def run() -> None:
        await service.cancel(_build_cancel_intent())
        await asyncio.sleep(0)

    asyncio.run(run())
    assert len(received) == 1
    assert received[0].event == LifecycleEvent.ORDER_REJECTED


def test_replace_full_fill_publishes_order_cancelled() -> None:
    """replace 路径下 FULL_FILL 也映射为 ORDER_CANCELLED——_publish_lifecycle 规定:
    cancel/replace 操作不能发 ORDER_FILLED/SUBMITTED，避免给策略噪音误以为新挂单。"""
    bus = InProcessLifecycleBus()
    received = _collect(bus, LifecycleEvent.ORDER_CANCELLED)
    executor = _StubControlExecutor(replace_status=OrderResultStatus.FULL_FILL)
    service = TradingService(risk_manager=_AlwaysPassRisk(), executor=executor, lifecycle_bus=bus)

    async def run() -> None:
        await service.replace(_build_replace_intent())
        await asyncio.sleep(0)

    asyncio.run(run())
    assert [kind for kind, _ in executor.calls] == ["replace"]
    assert len(received) == 1
    assert received[0].event == LifecycleEvent.ORDER_CANCELLED


def test_cancel_does_not_invoke_risk_manager() -> None:
    """CLAUDE.md §3：cancel/replace 走控制路径，不应过 RiskManager（风控只看新下单）。"""

    class _SpyRisk(RiskManager):
        def __init__(self) -> None:
            super().__init__()
            self.called = False

        def check_order_intent(self, *args, **kwargs) -> RiskDecision:  # type: ignore[override]
            self.called = True
            return RiskDecision(passed=True)

    spy = _SpyRisk()
    service = TradingService(
        risk_manager=spy,
        executor=_StubControlExecutor(cancel_status=OrderResultStatus.CANCELLED),
    )

    asyncio.run(service.cancel(_build_cancel_intent()))
    assert spy.called is False, "cancel 路径不应触发 RiskManager.check_order_intent"


def test_no_fill_does_not_publish_lifecycle() -> None:
    """NO_FILL 是"等待状态"，CLAUDE.md §3 + _publish_lifecycle 注释要求不发事件，
    避免给策略噪音。LIVE / NO_FILL 都不发。"""
    bus = InProcessLifecycleBus()
    received: list[LifecycleEnvelope] = []

    async def collect_any(envelope: LifecycleEnvelope) -> None:
        received.append(envelope)

    for evt in (LifecycleEvent.ORDER_FILLED, LifecycleEvent.ORDER_SUBMITTED, LifecycleEvent.ORDER_REJECTED, LifecycleEvent.ORDER_CANCELLED):
        bus.subscribe(evt, collect_any)

    service = TradingService(
        risk_manager=_AlwaysPassRisk(),
        executor=_StubExecutor(OrderResultStatus.NO_FILL),
        lifecycle_bus=bus,
    )

    async def run() -> None:
        await service.review_intent(_build_intent(), operation="buy")
        await asyncio.sleep(0)

    asyncio.run(run())
    # NO_FILL 不映射到任何 lifecycle event
    assert received == []


def test_live_order_publishes_order_submitted() -> None:
    """LIVE 状态 → ORDER_SUBMITTED（GTC 挂单等待成交）。"""
    bus = InProcessLifecycleBus()
    received = _collect(bus, LifecycleEvent.ORDER_SUBMITTED)
    service = TradingService(
        risk_manager=_AlwaysPassRisk(),
        executor=_StubExecutor(OrderResultStatus.LIVE),
        lifecycle_bus=bus,
    )

    async def run() -> None:
        await service.review_intent(_build_intent(), operation="buy")
        await asyncio.sleep(0)

    asyncio.run(run())
    assert len(received) == 1
    assert received[0].event == LifecycleEvent.ORDER_SUBMITTED


def test_executor_unavailable_returns_failed_result() -> None:
    """executor=None → 不挂，返回 FAILED + executor_unavailable reason，让上游能识别。"""
    service = TradingService(risk_manager=_AlwaysPassRisk(), executor=None)

    result = asyncio.run(service.review_intent(_build_intent(), operation="buy"))
    assert result.order_result is not None
    assert result.order_result.status == OrderResultStatus.FAILED
    assert result.order_result.reason == "executor_unavailable"
    assert result.submitted is False


def test_intent_tags_propagated_in_lifecycle_payload() -> None:
    """intent_tags 必须透传到 lifecycle payload 让策略侧能区分 scale_in / retry / 等。"""
    bus = InProcessLifecycleBus()
    received = _collect(bus, LifecycleEvent.ORDER_FILLED)
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
    assert received[0].payload["intent_tags"] == ("scale_in",)


def test_executor_raises_exception_returns_failed_result() -> None:
    """executor.submit() 抛异常 → TradingService 捕获并返回 FAILED，不让异常逃出链路。

    覆盖 trading_service.py 中 _execute_intent 的 except Exception 分支。
    """

    class _RaisingExecutor:
        async def submit(self, intent: BuyOrderIntent) -> OrderResult:
            raise RuntimeError("simulated_executor_crash")

    service = TradingService(
        risk_manager=_AlwaysPassRisk(),
        executor=_RaisingExecutor(),
    )

    result = asyncio.run(service.review_intent(_build_intent(), operation="buy"))
    assert result.order_result is not None
    assert result.order_result.status == OrderResultStatus.FAILED
    assert result.order_result.reason == "simulated_executor_crash"
