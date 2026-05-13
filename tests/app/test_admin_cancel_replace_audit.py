"""C-2 cancel / replace admin path audit emission."""
from __future__ import annotations

import asyncio
from decimal import Decimal
from types import SimpleNamespace
from typing import Any

import pytest

from polymarket_trader.app.admin_service import AdminService
from polymarket_trader.app.trading_service import TradingReviewResult
from polymarket_trader.domain.events import DomainEventType
from polymarket_trader.domain.market import Market, MarketOutcome, TradingStatus
from polymarket_trader.domain.order import (
    CancelOrderIntent,
    OrderResult,
    OrderResultStatus,
    OrderSide,
    OrderStatus,
    OrderType,
    OrderRecord,
    ReplaceOrderIntent,
)
from polymarket_trader.runtime.account_state import AccountStateStore
from polymarket_trader.runtime.registry import MarketRegistry


class _RecordingEventBus:
    def __init__(self) -> None:
        self.published: list[tuple[Any, Any]] = []

    def publish_nowait(self, priority: Any, event: Any) -> None:
        self.published.append((priority, event))

    async def publish(self, priority: Any, event: Any) -> None:
        self.published.append((priority, event))


@pytest.fixture()
def market() -> Market:
    return Market(
        condition_id="0xcond",
        market_slug="test-market",
        outcomes=(
            MarketOutcome(token_id="tok-yes", outcome="YES"),
            MarketOutcome(token_id="tok-no", outcome="NO"),
        ),
        trading_status=TradingStatus.ELIGIBLE,
        tick_size=Decimal("0.01"),
        min_order_size=Decimal("5"),
    )


@pytest.fixture()
def open_order() -> OrderRecord:
    return OrderRecord(
        strategy_id="sports_tail",
        condition_id="0xcond",
        token_id="tok-yes",
        side=OrderSide.BUY,
        order_type=OrderType.FAK,
        price=Decimal("0.42"),
        market_slug="test-market",
        size_shares=Decimal("100"),
        remaining_shares=Decimal("100"),
        order_id="order-1",
        status=OrderStatus.LIVE,
    )


class _StubTradingService:
    """Cancel / replace 都返回 success；记录 intent 便于断言。"""

    def __init__(self) -> None:
        self.last_cancel: CancelOrderIntent | None = None
        self.last_replace: ReplaceOrderIntent | None = None

    async def cancel(self, intent: CancelOrderIntent, **kwargs: Any) -> TradingReviewResult:
        self.last_cancel = intent
        return TradingReviewResult(
            intent=intent,
            operation="cancel",
            risk_decision=None,
            submitted=True,
            order_result=OrderResult(
                strategy_id="sports_tail",
                trace_id=intent.trace_id,
                condition_id=intent.condition_id,
                token_id=intent.token_id,
                status=OrderResultStatus.CANCELLED,
                order_id=intent.order_id,
                reason="cancelled_by_admin",
            ),
        )

    async def replace(self, intent: ReplaceOrderIntent, **kwargs: Any) -> TradingReviewResult:
        self.last_replace = intent
        return TradingReviewResult(
            intent=intent,
            operation="replace",
            risk_decision=None,
            submitted=True,
            order_result=OrderResult(
                strategy_id="sports_tail",
                trace_id=intent.trace_id,
                condition_id=intent.condition_id,
                token_id=intent.token_id,
                status=OrderResultStatus.LIVE,
                order_id="order-1-replaced",
                reason="replaced",
            ),
        )


def _build_runtime(
    *,
    market: Market,
    open_order: OrderRecord,
    event_bus: _RecordingEventBus,
    trading_service: _StubTradingService,
) -> SimpleNamespace:
    registry = MarketRegistry()
    registry.upsert(market)
    account_state = AccountStateStore()
    account_state.upsert_order(open_order)
    account_state.update_balances(balance_usdc=Decimal("100"), allowance_usdc=Decimal("100"))
    return SimpleNamespace(
        extension=SimpleNamespace(spec=SimpleNamespace(strategy_id="sports_tail")),
        registry=registry,
        account_state_store=account_state,
        event_bus=event_bus,
        trading_service=trading_service,
    )


def test_cancel_order_publishes_request_and_cancelled_events(
    market: Market, open_order: OrderRecord
) -> None:
    event_bus = _RecordingEventBus()
    trading_service = _StubTradingService()
    runtime = _build_runtime(
        market=market,
        open_order=open_order,
        event_bus=event_bus,
        trading_service=trading_service,
    )
    service = AdminService(runtime=runtime)

    result = asyncio.run(
        service.cancel_order(order_id="order-1", operator="ops", reason="cleanup")
    )

    assert result["status"] == "ok"
    assert trading_service.last_cancel is not None
    # 必须 publish 2 个事件：意图 + 结果
    event_types = [event.event_type for _, event in event_bus.published]
    assert DomainEventType.ORDER_CANCEL_REQUESTED in event_types
    assert DomainEventType.ORDER_CANCELLED in event_types
    # 意图 event 含 operator + reason + order_id
    req_event = next(e for _, e in event_bus.published if e.event_type == DomainEventType.ORDER_CANCEL_REQUESTED)
    assert req_event.payload["operator"] == "ops"
    assert req_event.payload["reason"] == "cleanup"
    assert req_event.payload["order_id"] == "order-1"
    # 结果 event 含 result_status
    cancelled_event = next(e for _, e in event_bus.published if e.event_type == DomainEventType.ORDER_CANCELLED)
    assert cancelled_event.payload["result_status"] == OrderResultStatus.CANCELLED.value
