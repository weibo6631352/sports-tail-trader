from __future__ import annotations

import asyncio
from datetime import datetime, timezone
from decimal import Decimal
from typing import Any

from polymarket_trader.app.extension_intent_builder import decision_to_managed_intent
from polymarket_trader.app.entry_plan import EntryPlan
from polymarket_trader.app.trading_service import TradingService
from polymarket_trader.domain.allocation import Allocation, AllocationPlan
from polymarket_trader.domain.events import DomainEvent, DomainEventType
from polymarket_trader.domain.account import MarketPauseSource
from polymarket_trader.domain.market import Market, MarketOutcome, TradingStatus
from polymarket_trader.domain.order import BuyOrderIntent, ManagedOrderIntent, OrderResult, OrderResultStatus, OrderSide
from polymarket_trader.domain.orderbook import OrderbookSnapshot, PriceLevel
from polymarket_trader.domain.position import Position
from polymarket_trader.extension_api import DecisionKind, ExtensionContext, ExtensionDecision
from polymarket_trader.runtime.account_state import AccountStateStore
from polymarket_trader.domain.state_machine import MarketLifecycle
from polymarket_trader.workers.trading_decision import TradingDecisionWorker


class _FailingDecisionService:
    strategy_id = "sports_tail"

    def build_entry_plan(self, **_: Any) -> Any:
        raise AssertionError("entry plan should not be built while entry gate is closed")


class _CountingDecisionService:
    strategy_id = "sports_tail"

    def __init__(self) -> None:
        self.calls = 0

    def build_entry_plan(self, **kwargs: Any) -> EntryPlan:
        self.calls += 1
        trace_id = str(kwargs.get("trace_id") or "trace")
        return EntryPlan(
            trace_id=trace_id,
            market=None,
            orderbook=None,
            allocation_plan=AllocationPlan(trace_id=trace_id, total_budget_usdc=Decimal("0")),
            allocation=None,
            intent=None,
            reason="missing_market_state",
        )


class _ExitDecisionService:
    strategy_id = "sports_tail"

    def __init__(self, market: Market, orderbook: OrderbookSnapshot) -> None:
        self.market = market
        self.orderbook = orderbook
        self.exit_calls = 0

    def build_entry_plan(self, **_: Any) -> Any:
        raise AssertionError("entry plan should not be built for position exit")

    def resolve_market(self, *, condition_id: str | None, token_id: str | None) -> Market | None:
        if condition_id == self.market.condition_id or token_id in self.market.token_ids:
            return self.market
        return None

    def lookup_orderbook(self, token_id: str) -> OrderbookSnapshot | None:
        return self.orderbook if token_id == self.orderbook.token_id else None

    def decide_exit(self, context: ExtensionContext) -> ExtensionDecision:
        self.exit_calls += 1
        if context.position is None:
            return ExtensionDecision.skip(reason="missing_position_state")
        uncovered = context.position.shares - context.position.open_sell_shares
        if uncovered <= Decimal("0"):
            return ExtensionDecision.skip(reason="no_uncovered_shares")
        return ExtensionDecision.sell(
            reason="strategy_exit",
            token_id=context.position.token_id,
            price=Decimal("0.99"),
            size_shares=uncovered,
            market_slug=None if context.market is None else context.market.market_slug,
            metadata={"exit_source": "unit_test"},
        )

    def decide_follow_up(self, context: ExtensionContext) -> tuple[ExtensionDecision, ...]:
        if context.order_result is None or context.order_result.side != OrderSide.BUY:
            return ()
        if context.order_result.matched_shares <= Decimal("0"):
            return ()
        return (
            ExtensionDecision.sell(
                reason="strategy_exit",
                token_id=context.order_result.token_id,
                price=Decimal("0.99"),
                size_shares=context.order_result.matched_shares,
                market_slug=self.market.market_slug,
                metadata={"exit_source": "unit_test_follow_up"},
            ),
        )

    def build_intent_from_decision(
        self,
        *,
        trace_id: str,
        condition_id: str,
        market_slug: str | None,
        default_token_id: str | None,
        decision: ExtensionDecision,
    ) -> ManagedOrderIntent | None:
        return decision_to_managed_intent(
            trace_id=trace_id,
            strategy_id="sports_tail",
            condition_id=condition_id,
            market_slug=market_slug,
            default_token_id=default_token_id,
            decision=decision,
        )


class _ScaleInDecisionService:
    strategy_id = "sports_tail"

    def __init__(self, market: Market, orderbook: OrderbookSnapshot) -> None:
        self.market = market
        self.orderbook = orderbook

    def build_entry_plan(self, **kwargs: Any) -> EntryPlan:
        trace_id = str(kwargs.get("trace_id") or "trace-scale")
        allocation = Allocation(
            strategy_id="sports_tail",
            condition_id=self.market.condition_id,
            token_id=self.orderbook.token_id,
            market_slug=self.market.market_slug,
            target_budget_usdc=Decimal("3"),
            buy_budget_usdc=Decimal("3"),
        )
        allocation_plan = AllocationPlan(
            trace_id=trace_id,
            total_budget_usdc=Decimal("5"),
            allocations=(allocation,),
        )
        intent = BuyOrderIntent(
            strategy_id="sports_tail",
            trace_id=trace_id,
            condition_id=self.market.condition_id,
            token_id=self.orderbook.token_id,
            price=Decimal("0.99"),
            amount_usdc=Decimal("3"),
            market_slug=self.market.market_slug,
            allow_open_exit_overlap=True,
            intent_tags=frozenset({"scale_in"}),
        )
        return EntryPlan(
            trace_id=trace_id,
            market=self.market,
            orderbook=self.orderbook,
            allocation_plan=allocation_plan,
            allocation=allocation,
            intent=intent,
            reason="strategy_scale_in",
            decision_kind=DecisionKind.SCALE_IN,
        )

    def resolve_market(self, *, condition_id: str | None, token_id: str | None) -> Market | None:
        if condition_id == self.market.condition_id or token_id in self.market.token_ids:
            return self.market
        return None

    def lookup_orderbook(self, token_id: str) -> OrderbookSnapshot | None:
        return self.orderbook if token_id == self.orderbook.token_id else None

    def decide_follow_up(self, context: ExtensionContext) -> tuple[ExtensionDecision, ...]:
        return ()


class _RetryableEntryDecisionService:
    strategy_id = "sports_tail"

    def __init__(self, market: Market, orderbooks: tuple[OrderbookSnapshot, ...]) -> None:
        self.market = market
        self.orderbooks = orderbooks
        self.calls = 0

    def build_entry_plan(self, **kwargs: Any) -> EntryPlan:
        index = min(self.calls, len(self.orderbooks) - 1)
        self.calls += 1
        orderbook = self.orderbooks[index]
        trace_id = str(kwargs.get("trace_id") or f"trace-retry-{self.calls}")
        allocation = Allocation(
            strategy_id="sports_tail",
            condition_id=self.market.condition_id,
            token_id=orderbook.token_id,
            market_slug=self.market.market_slug,
            target_budget_usdc=Decimal("5"),
            buy_budget_usdc=Decimal("5"),
        )
        allocation_plan = AllocationPlan(
            trace_id=trace_id,
            total_budget_usdc=Decimal("5"),
            allocations=(allocation,),
        )
        intent = BuyOrderIntent(
            strategy_id="sports_tail",
            trace_id=trace_id,
            condition_id=self.market.condition_id,
            token_id=orderbook.token_id,
            price=Decimal("0.99"),
            amount_usdc=Decimal("5"),
            market_slug=self.market.market_slug,
        )
        return EntryPlan(
            trace_id=trace_id,
            market=self.market,
            orderbook=orderbook,
            allocation_plan=allocation_plan,
            allocation=allocation,
            intent=intent,
            reason="retryable_entry",
        )

    def resolve_market(self, *, condition_id: str | None, token_id: str | None) -> Market | None:
        if condition_id == self.market.condition_id or token_id in self.market.token_ids:
            return self.market
        return None

    def lookup_orderbook(self, token_id: str) -> OrderbookSnapshot | None:
        for orderbook in self.orderbooks:
            if orderbook.token_id == token_id:
                return orderbook
        return None

    def decide_follow_up(self, context: ExtensionContext) -> tuple[ExtensionDecision, ...]:
        return ()


class _LiveSellExecutor:
    def __init__(self) -> None:
        self.intents: list[ManagedOrderIntent] = []

    async def submit(self, intent: ManagedOrderIntent) -> OrderResult:
        self.intents.append(intent)
        return OrderResult(
            strategy_id="sports_tail",
            trace_id=intent.trace_id,
            condition_id=intent.condition_id,
            token_id=intent.token_id,
            market_slug=intent.market_slug,
            status=OrderResultStatus.LIVE,
            intent=intent,
            order_id=f"sell-{intent.trace_id}",
            side=getattr(intent, "side", None),
            order_type=getattr(intent, "order_type", None),
            price=getattr(intent, "price", None),
            requested_size_shares=getattr(intent, "size_shares", None),
            remaining_shares=getattr(intent, "size_shares", Decimal("0")) or Decimal("0"),
            notional_usdc=getattr(intent, "notional_usdc", Decimal("0")),
            reason="paper_sell_live",
        )


class _NoFillBuyExecutor:
    def __init__(self) -> None:
        self.intents: list[ManagedOrderIntent] = []

    async def submit(self, intent: ManagedOrderIntent) -> OrderResult:
        self.intents.append(intent)
        return OrderResult(
            strategy_id="sports_tail",
            trace_id=intent.trace_id,
            condition_id=intent.condition_id,
            token_id=intent.token_id,
            market_slug=intent.market_slug,
            status=OrderResultStatus.NO_FILL,
            intent=intent,
            order_id=f"buy-{intent.trace_id}",
            side=getattr(intent, "side", None),
            order_type=getattr(intent, "order_type", None),
            price=getattr(intent, "price", None),
            requested_amount_usdc=getattr(intent, "amount_usdc", None),
            notional_usdc=getattr(intent, "notional_usdc", Decimal("0")),
            reason="test_no_fill",
        )


def test_orderbook_event_is_skipped_before_sizing_when_account_gate_closed() -> None:
    async def run() -> None:
        worker = TradingDecisionWorker(
            trading_decision_service=_FailingDecisionService(),
            account_state_store=AccountStateStore(),
        )

        result = await worker.process_event(_orderbook_event())

        assert result is None

    asyncio.run(run())


def test_orderbook_event_is_skipped_before_sizing_when_market_is_paused() -> None:
    async def run() -> None:
        account_state = AccountStateStore()
        account_state.mark_user_ws_connected(True)
        account_state.mark_reconciled()
        account_state.pause_market(
            "condition-1",
            reason="manual_pause",
            source=MarketPauseSource.MANUAL,
        )
        worker = TradingDecisionWorker(
            trading_decision_service=_FailingDecisionService(),
            account_state_store=account_state,
        )

        result = await worker.process_event(_orderbook_event())

        assert result is None

    asyncio.run(run())


def test_orderbook_events_are_not_dropped_by_decision_worker() -> None:
    async def run() -> None:
        account_state = _open_entry_gate()
        decision_service = _CountingDecisionService()
        worker = TradingDecisionWorker(
            trading_decision_service=decision_service,
            account_state_store=account_state,
        )

        await worker.process_event(_orderbook_event(event_id="event-orderbook-1"))
        await worker.process_event(_orderbook_event(event_id="event-orderbook-2"))

        assert decision_service.calls == 2

    asyncio.run(run())


def test_explicit_entry_signals_are_not_rate_limited() -> None:
    async def run() -> None:
        account_state = _open_entry_gate()
        decision_service = _CountingDecisionService()
        worker = TradingDecisionWorker(
            trading_decision_service=decision_service,
            account_state_store=account_state,
        )

        await worker.process_event(_entry_signal_event(event_id="event-entry-1"))
        await worker.process_event(_entry_signal_event(event_id="event-entry-2"))

        assert decision_service.calls == 2

    asyncio.run(run())


def test_position_update_places_exit_order_for_uncovered_position() -> None:
    async def run() -> None:
        market, orderbook = _exit_market_and_orderbook()
        account_state = AccountStateStore()
        account_state.upsert_position(_position(shares=Decimal("7"), open_sell_shares=Decimal("0")))
        decision_service = _ExitDecisionService(market, orderbook)
        executor = _LiveSellExecutor()
        worker = TradingDecisionWorker(
            trading_decision_service=decision_service,
            trading_service=TradingService(executor=executor),
            account_state_store=account_state,
        )

        result = await worker.process_event(_position_event())

        assert result is not None
        assert len(result.follow_up_intents) == 1
        assert result.follow_up_intents[0].side == OrderSide.SELL
        assert result.follow_up_reviews[0].risk_decision.passed
        snapshot = account_state.snapshot()
        position = snapshot.get_position("condition-1", "token-1")
        assert position is not None
        assert position.shares == Decimal("7")
        assert position.open_sell_shares == Decimal("7")
        assert len(snapshot.open_orders) == 1

    asyncio.run(run())


def test_user_ws_projected_buy_fill_triggers_exit_without_double_counting_position() -> None:
    async def run() -> None:
        market, orderbook = _exit_market_and_orderbook()
        account_state = AccountStateStore()
        account_state.upsert_position(_position(shares=Decimal("7"), open_sell_shares=Decimal("0")))
        decision_service = _ExitDecisionService(market, orderbook)
        executor = _LiveSellExecutor()
        worker = TradingDecisionWorker(
            trading_decision_service=decision_service,
            trading_service=TradingService(executor=executor),
            account_state_store=account_state,
        )

        result = await worker.process_event(_projected_user_ws_buy_fill_event())

        assert result is not None
        assert len(result.follow_up_intents) == 1
        snapshot = account_state.snapshot()
        position = snapshot.get_position("condition-1", "token-1")
        assert position is not None
        assert position.shares == Decimal("7")
        assert position.open_sell_shares == Decimal("7")

    asyncio.run(run())


def test_entry_signal_scale_in_plan_is_not_dropped_while_exit_order_is_open() -> None:
    async def run() -> None:
        market, orderbook = _exit_market_and_orderbook()
        account_state = _open_entry_gate()
        account_state.update_balances(balance_usdc=Decimal("10"), allowance_usdc=Decimal("10"))
        decision_service = _ScaleInDecisionService(market, orderbook)
        executor = _LiveSellExecutor()
        worker = TradingDecisionWorker(
            trading_decision_service=decision_service,
            trading_service=TradingService(executor=executor),
            account_state_store=account_state,
            max_order_usdc=Decimal("5"),
            max_market_usdc=Decimal("20"),
            max_total_usdc=Decimal("20"),
        )
        worker._market_lifecycle[market.condition_id] = MarketLifecycle.FOLLOW_UP_ORDER_OPEN

        result = await worker.process_event(_entry_signal_event(event_id="event-scale-in"))

        assert result is not None
        assert result.plan is not None
        assert result.plan.intent is not None
        assert result.plan.intent.side == OrderSide.BUY
        assert result.review is not None
        assert result.review.risk_decision.passed

    asyncio.run(run())


def test_entry_signal_scale_in_plan_is_dropped_while_market_lifecycle_is_paused() -> None:
    async def run() -> None:
        market, orderbook = _exit_market_and_orderbook()
        account_state = _open_entry_gate()
        account_state.update_balances(balance_usdc=Decimal("10"), allowance_usdc=Decimal("10"))
        decision_service = _ScaleInDecisionService(market, orderbook)
        executor = _LiveSellExecutor()
        worker = TradingDecisionWorker(
            trading_decision_service=decision_service,
            trading_service=TradingService(executor=executor),
            account_state_store=account_state,
            max_order_usdc=Decimal("5"),
            max_market_usdc=Decimal("20"),
            max_total_usdc=Decimal("20"),
        )
        worker._market_lifecycle[market.condition_id] = MarketLifecycle.PAUSED

        result = await worker.process_event(_entry_signal_event(event_id="event-scale-in-paused"))

        assert result is None
        assert executor.intents == []

    asyncio.run(run())


def test_worker_emits_heartbeat_after_processing_event_and_on_idle() -> None:
    """N13：worker 必须能让 supervisor 区分「卡死」和「空闲等事件」。

    场景：
    - bind_heartbeat 注入 fake supervisor 回调
    - 推一个 ORDERBOOK_SNAPSHOT_UPDATED 事件 → run_once 完成后 heartbeat
      detail 含 ``processed event_type=...``
    - 队列空 + 设极短 idle 超时 → run_once 自身阻塞，超时一次 → heartbeat
      detail 含 ``idle``，证明 worker 在动而不是卡死
    """

    async def run() -> None:
        from polymarket_trader.domain.events import OutboxPriority
        from polymarket_trader.runtime.event_bus import EventBus

        account_state = _open_entry_gate()
        decision_service = _CountingDecisionService()
        event_bus = EventBus()
        worker = TradingDecisionWorker(
            event_bus=event_bus,
            trading_decision_service=decision_service,
            account_state_store=account_state,
            idle_heartbeat_seconds=0.05,
        )
        heartbeats: list[dict[str, Any]] = []

        def _capture(**kwargs: Any) -> None:
            heartbeats.append(kwargs)

        worker.bind_heartbeat(_capture)

        # 推一个事件，run_once 应该立刻处理并发心跳。
        await event_bus.publish(
            OutboxPriority.P1,
            _orderbook_event(event_id="event-heartbeat-1"),
        )
        await worker.run_once()
        assert decision_service.calls == 1
        assert any("processed" in str(hb.get("detail", "")) for hb in heartbeats), heartbeats
        assert any(
            DomainEventType.ORDERBOOK_SNAPSHOT_UPDATED.value in str(hb.get("detail", ""))
            for hb in heartbeats
        ), heartbeats

        # 空闲：run_once 阻塞，应在 idle_heartbeat_seconds 超时后发出 idle 心跳；为避免
        # 单测在 worker 内部死循环里卡死，外层加 0.3s 超时然后取消。
        heartbeats.clear()
        task = asyncio.create_task(worker.run_once())
        try:
            await asyncio.sleep(0.2)
        finally:
            task.cancel()
            try:
                await task
            except asyncio.CancelledError:
                pass

        assert any("idle" in str(hb.get("detail", "")) for hb in heartbeats), heartbeats

    asyncio.run(run())


def test_retryable_entry_rejection_keeps_market_observable_for_next_signal() -> None:
    async def run() -> None:
        market, _ = _exit_market_and_orderbook()
        insufficient_orderbook = _orderbook_with_ask_size(size=Decimal("1"))
        sufficient_orderbook = _orderbook_with_ask_size(size=Decimal("20"))
        account_state = _open_entry_gate()
        account_state.update_balances(balance_usdc=Decimal("10"), allowance_usdc=Decimal("10"))
        decision_service = _RetryableEntryDecisionService(
            market,
            (insufficient_orderbook, sufficient_orderbook),
        )
        executor = _NoFillBuyExecutor()
        worker = TradingDecisionWorker(
            trading_decision_service=decision_service,
            trading_service=TradingService(executor=executor),
            account_state_store=account_state,
            max_order_usdc=Decimal("5"),
            max_market_usdc=Decimal("20"),
            max_total_usdc=Decimal("20"),
        )

        first_result = await worker.process_event(_entry_signal_event(event_id="event-retryable-1"))
        second_result = await worker.process_event(_entry_signal_event(event_id="event-retryable-2"))

        assert first_result is not None
        assert first_result.review is not None
        assert first_result.review.risk_decision.reason == "liquidity_insufficient"
        assert second_result is not None
        assert second_result.review is not None
        assert second_result.review.risk_decision.passed
        assert len(executor.intents) == 1

    asyncio.run(run())


def _orderbook_event(*, event_id: str = "event-orderbook") -> DomainEvent:
    return _event(DomainEventType.ORDERBOOK_SNAPSHOT_UPDATED, event_id=event_id)


def _entry_signal_event(*, event_id: str) -> DomainEvent:
    return _event(DomainEventType.ENTRY_SIGNAL_TRIGGERED, event_id=event_id)


def _event(event_type: DomainEventType, *, event_id: str) -> DomainEvent:
    return DomainEvent(
        trace_id="trace-orderbook",
        event_type=event_type,
        event_id=event_id,
        market_slug="nba-game-moneyline",
        condition_id="condition-1",
        token_id="token-1",
        payload={"source": "test"},
    )


def _open_entry_gate() -> AccountStateStore:
    account_state = AccountStateStore()
    account_state.mark_user_ws_connected(True)
    account_state.mark_reconciled()
    return account_state


def _exit_market_and_orderbook() -> tuple[Market, OrderbookSnapshot]:
    market = Market(
        condition_id="condition-1",
        market_slug="wta-player-a-player-b-2026-04-28",
        outcomes=(MarketOutcome(token_id="token-1", outcome="Player A"),),
        trading_status=TradingStatus.ELIGIBLE,
        min_order_size=Decimal("1"),
        tick_size=Decimal("0.01"),
    )
    orderbook = OrderbookSnapshot(
        token_id="token-1",
        best_bid=Decimal("0.98"),
        best_ask=Decimal("0.99"),
        bids=(PriceLevel(price=Decimal("0.98"), size=Decimal("20")),),
        asks=(PriceLevel(price=Decimal("0.99"), size=Decimal("20")),),
        received_at=datetime(2026, 4, 28, tzinfo=timezone.utc),
        tick_size=Decimal("0.01"),
        market_slug=market.market_slug,
        condition_id=market.condition_id,
    )
    return market, orderbook


def _orderbook_with_ask_size(*, size: Decimal) -> OrderbookSnapshot:
    market, orderbook = _exit_market_and_orderbook()
    return OrderbookSnapshot(
        token_id=orderbook.token_id,
        best_bid=Decimal("0.98"),
        best_ask=Decimal("0.99"),
        best_bid_size=Decimal("20"),
        best_ask_size=size,
        bids=(PriceLevel(price=Decimal("0.98"), size=Decimal("20")),),
        asks=(PriceLevel(price=Decimal("0.99"), size=size),),
        received_at=datetime(2026, 4, 28, tzinfo=timezone.utc),
        tick_size=Decimal("0.01"),
        market_slug=market.market_slug,
        condition_id=market.condition_id,
    )


def _position(*, shares: Decimal, open_sell_shares: Decimal) -> Position:
    return Position(
        strategy_id="sports_tail",
        condition_id="condition-1",
        token_id="token-1",
        market_slug="wta-player-a-player-b-2026-04-28",
        shares=shares,
        cost_usdc=Decimal("5"),
        open_sell_shares=open_sell_shares,
    )


def _position_event() -> DomainEvent:
    return DomainEvent(
        trace_id="trace-position",
        event_type=DomainEventType.POSITION_UPDATED,
        event_id="event-position",
        market_slug="wta-player-a-player-b-2026-04-28",
        condition_id="condition-1",
        token_id="token-1",
        reason="position_after_fill",
        payload={
            "position": {
                "condition_id": "condition-1",
                "token_id": "token-1",
                "shares": "7",
                "open_sell_shares": "0",
            },
        },
    )


def _projected_user_ws_buy_fill_event() -> DomainEvent:
    return DomainEvent(
        trace_id="trace-fill",
        event_type=DomainEventType.ORDER_STATE_UPDATED,
        event_id="event-fill-order",
        market_slug="wta-player-a-player-b-2026-04-28",
        condition_id="condition-1",
        token_id="token-1",
        reason="matched",
        payload={
            "position_projected": True,
            "strategy_id": "sports_tail",
            "order": {
                "strategy_id": "sports_tail",
                "trace_id": "trace-fill",
                "condition_id": "condition-1",
                "token_id": "token-1",
                "market_slug": "wta-player-a-player-b-2026-04-28",
                "side": "BUY",
                "order_type": "FAK",
                "price": "0.71",
                "amount_usdc": "5",
                "order_id": "buy-order-1",
                "status": "matched",
            },
            "fill": {
                "trace_id": "trace-fill",
                "condition_id": "condition-1",
                "token_id": "token-1",
                "market_slug": "wta-player-a-player-b-2026-04-28",
                "side": "buy",
                "price": "0.71",
                "size": "7",
                "notional_usdc": "5",
                "order_id": "buy-order-1",
                "trade_id": "trade-1",
                "status": "confirmed",
            },
        },
    )
