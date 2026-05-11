"""Cover the four manual-intervention admin endpoints introduced for frontend operators.

Each method must walk through TradingService / RiskManager / OrderExecutor or
Supervisor / AccountStateStore the same way automated paths do—never a bypass.
"""

from __future__ import annotations

import asyncio
from dataclasses import dataclass
from datetime import datetime, timezone
from decimal import Decimal
from types import SimpleNamespace
from typing import Any

import pytest

from polymarket_trader.app.admin_service import AdminService
from polymarket_trader.app.trading_service import TradingReviewResult
from polymarket_trader.domain.account import MarketPauseSource
from polymarket_trader.domain.market import (
    Market,
    MarketOutcome,
    TradingStatus,
)
from polymarket_trader.domain.order import (
    CancelOrderIntent,
    OrderResult,
    OrderResultStatus,
    OrderSide,
    OrderStatus,
    OrderType,
    OrderRecord,
    SellOrderIntent,
)
from polymarket_trader.domain.orderbook import OrderbookSnapshot
from polymarket_trader.domain.position import Position
from polymarket_trader.runtime.account_state import AccountStateStore
from polymarket_trader.runtime.event_bus import EventBus
from polymarket_trader.runtime.registry import MarketRegistry
from polymarket_trader.runtime.status import RuntimePhase
from polymarket_trader.runtime.supervisor import Supervisor


@dataclass
class _RecordingTradingService:
    """Records intents passed through TradingService to verify the service routes
    cancel / sell through the same gateway as the live path.
    """

    last_cancel: CancelOrderIntent | None = None
    last_sell: SellOrderIntent | None = None
    last_sell_kwargs: dict[str, Any] | None = None

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
            ),
        )

    async def sell(self, intent: SellOrderIntent, **kwargs: Any) -> TradingReviewResult:
        self.last_sell = intent
        self.last_sell_kwargs = kwargs
        return TradingReviewResult(
            intent=intent,
            operation="sell",
            risk_decision=None,
            submitted=True,
            order_result=OrderResult(
                strategy_id="sports_tail",
                trace_id=intent.trace_id,
                condition_id=intent.condition_id,
                token_id=intent.token_id,
                status=OrderResultStatus.LIVE,
                side=OrderSide.SELL,
            ),
        )


def _build_supervisor() -> Supervisor:
    return Supervisor(
        event_bus=EventBus(max_size=10),
        settings_readiness=SimpleNamespace(as_dict=lambda: {"ready_to_trade": True}),
    )


def _build_market() -> Market:
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


def _build_registry(market: Market) -> MarketRegistry:
    registry = MarketRegistry()
    registry.upsert(market)
    return registry


def test_pause_and_resume_trading_drive_supervisor_phase() -> None:
    supervisor = _build_supervisor()
    supervisor._phase = RuntimePhase.TRADING_ENABLED
    service = AdminService(runtime=SimpleNamespace(supervisor=supervisor))

    pause = asyncio.run(service.pause_trading(reason="market_alarm", operator="op"))
    assert pause["status"] == "ok"
    assert pause["manual_pause_reason"] == "market_alarm"
    assert supervisor._phase == RuntimePhase.PAUSED

    resume = asyncio.run(service.resume_trading(operator="op"))
    assert resume["status"] == "ok"
    assert resume["manual_pause_reason"] is None
    assert supervisor._phase == RuntimePhase.TRADING_ENABLED


def test_pause_trading_returns_failed_when_supervisor_missing() -> None:
    service = AdminService(runtime=SimpleNamespace())
    result = asyncio.run(service.pause_trading(reason="manual_pause"))
    assert result == {"status": "failed", "reason": "supervisor_unavailable"}


def test_cancel_order_routes_through_trading_service() -> None:
    market = _build_market()
    registry = _build_registry(market)
    account_state = AccountStateStore()
    account_state.upsert_order(
        OrderRecord(
            strategy_id="sports_tail",
            condition_id=market.condition_id,
            token_id="tok-yes",
            side=OrderSide.SELL,
            order_type=OrderType.GTC,
            price=Decimal("0.6"),
            market_slug=market.market_slug,
            size_shares=Decimal("10"),
            remaining_shares=Decimal("10"),
            order_id="ord-1",
            status=OrderStatus.LIVE,
        )
    )
    trading_service = _RecordingTradingService()
    service = AdminService(
        runtime=SimpleNamespace(
            extension=SimpleNamespace(spec=SimpleNamespace(strategy_id="sports_tail")),
            registry=registry,
            account_state_store=account_state,
            trading_service=trading_service,
        )
    )

    result = asyncio.run(
        service.cancel_order(order_id="ord-1", operator="op", reason="user_abort", trace_id="trace-1")
    )

    assert result["status"] == "ok"
    assert trading_service.last_cancel is not None
    assert trading_service.last_cancel.order_id == "ord-1"
    assert trading_service.last_cancel.condition_id == market.condition_id
    assert trading_service.last_cancel.token_id == "tok-yes"
    assert trading_service.last_cancel.trace_id == "trace-1"


def test_cancel_order_returns_order_not_found_when_open_order_absent() -> None:
    market = _build_market()
    registry = _build_registry(market)
    account_state = AccountStateStore()
    service = AdminService(
        runtime=SimpleNamespace(
            extension=SimpleNamespace(spec=SimpleNamespace(strategy_id="sports_tail")),
            registry=registry,
            account_state_store=account_state,
            trading_service=_RecordingTradingService(),
        )
    )

    result = asyncio.run(service.cancel_order(order_id="missing"))
    assert result["status"] == "failed"
    assert result["reason"] == "order_not_found"


def test_force_exit_position_uses_best_bid_and_calls_trading_service_sell() -> None:
    market = _build_market()
    registry = _build_registry(market)
    account_state = AccountStateStore()
    account_state.upsert_position(
        Position(
            strategy_id="sports_tail",
            condition_id=market.condition_id,
            token_id="tok-yes",
            market_slug=market.market_slug,
            shares=Decimal("10"),
            cost_usdc=Decimal("5"),
        )
    )

    trading_service = _RecordingTradingService()
    orderbook = OrderbookSnapshot(
        token_id="tok-yes",
        best_bid=Decimal("0.55"),
        best_ask=Decimal("0.6"),
        bids=(),
        asks=(),
        received_at=datetime(2026, 5, 11, tzinfo=timezone.utc),
    )

    class _FakeMarketWs:
        def snapshot(self, token_id: str) -> OrderbookSnapshot | None:
            return orderbook if token_id == "tok-yes" else None

    service = AdminService(
        runtime=SimpleNamespace(
            extension=SimpleNamespace(spec=SimpleNamespace(strategy_id="sports_tail")),
            registry=registry,
            account_state_store=account_state,
            trading_service=trading_service,
            market_ws_worker=_FakeMarketWs(),
            settings=SimpleNamespace(
                max_order_usdc=Decimal("100"),
                max_market_usdc=Decimal("100"),
                max_total_usdc=Decimal("100"),
                max_open_orders=5,
                order_retry_limit=1,
            ),
        )
    )

    result = asyncio.run(
        service.force_exit_position(
            condition_id=market.condition_id,
            token_id="tok-yes",
            operator="op",
        )
    )

    assert result["status"] == "ok"
    sell = trading_service.last_sell
    assert sell is not None
    assert sell.price == Decimal("0.55")
    assert sell.size_shares == Decimal("10")
    assert sell.order_type == OrderType.GTC
    kwargs = trading_service.last_sell_kwargs or {}
    assert kwargs["market"] is not None
    assert kwargs["orderbook"] is orderbook


def test_force_exit_returns_position_not_found_for_zero_shares() -> None:
    market = _build_market()
    registry = _build_registry(market)
    account_state = AccountStateStore()
    service = AdminService(
        runtime=SimpleNamespace(
            extension=SimpleNamespace(spec=SimpleNamespace(strategy_id="sports_tail")),
            registry=registry,
            account_state_store=account_state,
            trading_service=_RecordingTradingService(),
        )
    )
    result = asyncio.run(
        service.force_exit_position(condition_id=market.condition_id, token_id="tok-yes")
    )
    assert result["status"] == "failed"
    assert result["reason"] == "position_not_found"


def test_force_exit_returns_best_bid_unavailable_when_orderbook_missing() -> None:
    market = _build_market()
    registry = _build_registry(market)
    account_state = AccountStateStore()
    account_state.upsert_position(
        Position(
            strategy_id="sports_tail",
            condition_id=market.condition_id,
            token_id="tok-yes",
            shares=Decimal("4"),
            cost_usdc=Decimal("2"),
        )
    )

    class _EmptyMarketWs:
        def snapshot(self, _token_id: str) -> OrderbookSnapshot | None:
            return None

    service = AdminService(
        runtime=SimpleNamespace(
            extension=SimpleNamespace(spec=SimpleNamespace(strategy_id="sports_tail")),
            registry=registry,
            account_state_store=account_state,
            trading_service=_RecordingTradingService(),
            market_ws_worker=_EmptyMarketWs(),
        )
    )
    result = asyncio.run(
        service.force_exit_position(condition_id=market.condition_id, token_id="tok-yes")
    )
    assert result["status"] == "failed"
    assert result["reason"] == "best_bid_unavailable"


def test_pause_and_resume_market_manual_update_account_state_pauses() -> None:
    account_state = AccountStateStore()
    service = AdminService(runtime=SimpleNamespace(account_state_store=account_state))

    pause = asyncio.run(
        service.pause_market_manual(condition_id="0xcond", reason="data_glitch", operator="op")
    )
    assert pause["status"] == "ok"
    snapshot = account_state.snapshot()
    pause_record = snapshot.pause_for_market("0xcond")
    assert pause_record is not None
    assert pause_record.source == MarketPauseSource.MANUAL
    assert pause_record.reason == "data_glitch"

    resume = asyncio.run(service.resume_market_manual(condition_id="0xcond", operator="op"))
    assert resume["status"] == "ok"
    assert account_state.snapshot().pause_for_market("0xcond") is None


def test_pause_market_manual_returns_failed_when_store_missing() -> None:
    service = AdminService(runtime=SimpleNamespace())
    result = asyncio.run(service.pause_market_manual(condition_id="0xcond"))
    assert result == {"status": "failed", "reason": "account_state_store_unavailable"}


if __name__ == "__main__":  # pragma: no cover
    pytest.main([__file__])
