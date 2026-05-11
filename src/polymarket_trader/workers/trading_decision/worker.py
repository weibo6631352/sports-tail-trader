from __future__ import annotations

from datetime import datetime, timezone
from decimal import Decimal
from typing import Callable, Iterable, Mapping
from uuid import uuid4

from polymarket_trader.app.trading_decision_service import EntryPlan, TradingDecisionService
from polymarket_trader.app.trading_service import TradingReviewResult, TradingService
from polymarket_trader.app.order_projection import AccountStateProjector
from polymarket_trader.domain.events import DomainEvent, DomainEventType, OutboxPriority
from polymarket_trader.domain.market import Market
from polymarket_trader.domain.order import (
    CancelOrderIntent,
    ManagedOrderIntent,
    Order,
    OrderResult,
    OrderResultStatus,
    ReplaceOrderIntent,
    SellOrderIntent,
)
from polymarket_trader.domain.position import Position
from polymarket_trader.domain.state_machine import MarketLifecycle
from polymarket_trader.domain.account import AccountSnapshot, MarketPauseSource
from polymarket_trader.extension_api import ExtensionAction, ExtensionContext, MarketTokenView
from polymarket_trader.runtime.account_state import AccountStateStore
from polymarket_trader.runtime.event_bus import EventBus
from .event_payloads import (
    TRADING_DECISION_WORKER_ORIGIN,
    coerce_order_result_from_event,
    is_self_emitted,
    market_from_result,
    serialize_allocation,
    serialize_allocation_plan,
    serialize_intent,
    serialize_plan_metadata,
    serialize_review,
    serialize_snapshot,
    snapshot_allowance,
    snapshot_available_usdc,
    snapshot_position,
)
from .order_result_processor import TradingOrderResultProcessor
from .result import TradingDecisionWorkerResult

PositionsProvider = Callable[[], Iterable[Position]]
OpenOrdersProvider = Callable[[], Iterable[Order]]
EntryMetadataProvider = Callable[[DomainEvent, AccountSnapshot | None], Mapping[str, object] | None]
POSITION_INCREASE_LIFECYCLES = {
    MarketLifecycle.POSITION_OPEN,
    MarketLifecycle.FOLLOW_UP_ORDER_OPEN,
}
ENTRY_ATTEMPT_LIFECYCLES = {
    MarketLifecycle.WATCHING_ORDERBOOK,
    MarketLifecycle.ENTRY_READY,
}


def _utc_now() -> datetime:
    return datetime.now(timezone.utc)


def _entry_gate_closed_for_event(
    snapshot: AccountSnapshot | None,
    event: DomainEvent,
) -> bool:
    """账户或单市场入场闸门关闭时，不对高频盘口事件构建交易计划。"""

    if snapshot is None:
        return False
    if not snapshot.allow_new_entries:
        return True
    if event.condition_id and snapshot.is_market_paused(event.condition_id):
        return True
    return False


class TradingDecisionWorker:
    priority = "P0"

    def __init__(
        self,
        *,
        event_bus: EventBus | None = None,
        trading_decision_service: TradingDecisionService | None = None,
        trading_service: TradingService | None = None,
        positions_provider: PositionsProvider | None = None,
        open_orders_provider: OpenOrdersProvider | None = None,
        account_state_store: AccountStateStore | None = None,
        portfolio_budget_usdc: Decimal = Decimal("0"),
        available_usdc: Decimal | None = None,
        max_order_usdc: Decimal = Decimal("0"),
        max_market_usdc: Decimal = Decimal("0"),
        max_total_usdc: Decimal = Decimal("0"),
        balance_usdc: Decimal | None = None,
        allowance_usdc: Decimal | None = None,
        max_open_orders: int | None = None,
        order_retry_limit: int | None = None,
        entry_metadata_provider: EntryMetadataProvider | None = None,
    ) -> None:
        self._event_bus = event_bus
        if trading_decision_service is None:
            raise ValueError("trading_decision_service is required")
        self._trading_decision_service = trading_decision_service
        self._trading_service = trading_service or TradingService()
        self._account_state_store = account_state_store
        self._positions_provider = positions_provider or self._build_positions_provider()
        self._open_orders_provider = open_orders_provider or self._build_open_orders_provider()
        self._portfolio_budget_usdc = portfolio_budget_usdc
        self._available_usdc = available_usdc
        self._max_order_usdc = max_order_usdc
        self._max_market_usdc = max_market_usdc
        self._max_total_usdc = max_total_usdc
        self._balance_usdc = balance_usdc
        self._allowance_usdc = allowance_usdc
        self._max_open_orders = max_open_orders
        self._order_retry_limit = order_retry_limit
        self._entry_metadata_provider = entry_metadata_provider
        self._market_lifecycle: dict[str, MarketLifecycle] = {}
        self._order_result_processor = TradingOrderResultProcessor(
            host=self,
            trading_decision_service=self._trading_decision_service,
            trading_service=self._trading_service,
            account_state_store=self._account_state_store,
        )

    async def run(self) -> None:
        if self._event_bus is None:
            raise RuntimeError("TradingDecisionWorker requires an EventBus to run")
        while True:
            await self.run_once()

    async def run_once(self) -> "TradingDecisionWorkerResult | None":
        if self._event_bus is None:
            raise RuntimeError("TradingDecisionWorker requires an EventBus to run")
        event = await self._event_bus.next_trading_event()
        return await self.process_event(event)

    async def process_event(self, event: DomainEvent) -> "TradingDecisionWorkerResult | None":
        if is_self_emitted(event):
            return None

        event_name = str(event.event_type)
        snapshot = self._snapshot()
        if event_name in {
            DomainEventType.ORDERBOOK_SNAPSHOT_UPDATED.value,
            DomainEventType.ENTRY_SIGNAL_TRIGGERED.value,
        }:
            return await self._handle_orderbook_snapshot_updated(event, snapshot)

        order_result = coerce_order_result_from_event(event)
        if order_result is not None:
            return await self._handle_order_result(
                source_event=event,
                order_result=order_result,
                snapshot=snapshot,
            )

        if event_name == DomainEventType.POSITION_UPDATED.value:
            return await self._handle_position_updated(event, snapshot)

        return None

    async def _handle_orderbook_snapshot_updated(
        self,
        event: DomainEvent,
        snapshot: AccountSnapshot | None,
    ) -> "TradingDecisionWorkerResult | None":
        if _entry_gate_closed_for_event(snapshot, event):
            return None
        plan = self._trading_decision_service.build_entry_plan(
            trace_id=event.trace_id,
            condition_id=event.condition_id,
            token_id=event.token_id,
            account_snapshot=snapshot,
            portfolio_budget_usdc=self._portfolio_budget_usdc,
            available_usdc=(
                self._available_usdc if self._available_usdc is not None else snapshot_available_usdc(snapshot)
            ),
            max_order_usdc=self._max_order_usdc,
            max_market_usdc=self._max_market_usdc,
            max_total_usdc=self._max_total_usdc,
            positions=(snapshot.positions if snapshot is not None else tuple(self._positions_provider())),
            open_orders=(
                snapshot.open_orders if snapshot is not None else tuple(self._open_orders_provider())
            ),
            metadata=self._entry_metadata(event, snapshot),
        )
        if plan.market is None or plan.orderbook is None or event.token_id != plan.orderbook.token_id:
            return None

        state = self._state_for_market(plan.market)
        if state is None:
            self._transition_market(plan.market, MarketLifecycle.WATCHING_ORDERBOOK)
        elif not _state_allows_entry_attempt(
            state,
            plan,
        ):
            return None
        return await self._execute_entry_plan(event=event, snapshot=snapshot, plan=plan)

    async def _execute_entry_plan(
        self,
        *,
        event: DomainEvent,
        snapshot: AccountSnapshot | None,
        plan: EntryPlan,
    ) -> "TradingDecisionWorkerResult":
        positions = snapshot.positions if snapshot is not None else tuple(self._positions_provider())
        open_orders = (
            snapshot.open_orders if snapshot is not None else tuple(self._open_orders_provider())
        )
        if snapshot is not None and not snapshot.allow_new_entries:
            return await self._emit_entry_skip(
                event=event,
                plan=plan,
                trace_id=event.trace_id,
                reason="entry_paused",
                lifecycle=MarketLifecycle.PAUSED,
                extra_payload={"account_snapshot": serialize_snapshot(snapshot)},
                emit_in_tuple=False,
            )

        if not plan.ready_to_trade or plan.intent is None or plan.market is None or plan.orderbook is None:
            return await self._emit_entry_skip(
                event=event,
                plan=plan,
                trace_id=plan.trace_id,
                reason=plan.reason or "entry_not_ready",
                lifecycle=MarketLifecycle.WATCHING_ORDERBOOK,
                extra_payload=None,
                emit_in_tuple=True,
            )

        focus_token_id = plan.intent.token_id
        focus_position = _match_position(positions, plan.market.condition_id, focus_token_id)
        focus_open_orders = _match_open_orders(open_orders, plan.market.condition_id, focus_token_id)
        self._transition_market(plan.market, MarketLifecycle.ENTRY_SUBMITTING)
        review = await self._trading_service.review_intent(
            plan.intent,
            market=plan.market,
            orderbook=plan.orderbook,
            position=focus_position,
            open_orders=focus_open_orders,
            allocation_plan=plan.allocation_plan,
            classification_passed=True,
            balance_usdc=self._balance_usdc if self._balance_usdc is not None else snapshot_available_usdc(snapshot),
            allowance_usdc=(
                self._allowance_usdc if self._allowance_usdc is not None else snapshot_allowance(snapshot)
            ),
            max_order_usdc=self._max_order_usdc,
            max_market_usdc=self._max_market_usdc,
            max_total_usdc=self._max_total_usdc,
            max_open_orders=self._max_open_orders,
            order_retry_limit=self._order_retry_limit,
            operation=plan.intent.side.value.lower(),
        )
        risk_event = await self._publish(
            DomainEventType.RISK_CHECK_PASSED
            if review.risk_decision is not None and review.risk_decision.passed
            else DomainEventType.RISK_CHECK_FAILED,
            trace_id=plan.trace_id,
            market_slug=plan.market.market_slug,
            condition_id=plan.market.condition_id,
            token_id=plan.intent.token_id,
            reason="" if review.risk_decision is None else review.risk_decision.reason,
            payload={
                "entry_event_id": event.event_id,
                "origin": TRADING_DECISION_WORKER_ORIGIN,
                "allocation_plan": serialize_allocation_plan(plan),
                "allocation": serialize_allocation(plan),
                "plan_metadata": serialize_plan_metadata(plan),
                "intent": serialize_intent(plan.intent),
                "review": serialize_review(review),
            },
        )
        result = await self._handle_order_result(
            source_event=event,
            order_result=review.order_result,
            snapshot=snapshot,
            execution=review,
            plan=plan,
        )
        return TradingDecisionWorkerResult(
            entry_event=event,
            plan=plan,
            review=review,
            emitted_event=risk_event,
            emitted_events=(risk_event, *result.emitted_events),
            follow_up_intents=result.follow_up_intents,
            follow_up_reviews=result.follow_up_reviews,
            state_before=result.state_before,
            state_after=result.state_after,
        )

    async def _handle_order_result(
        self,
        *,
        source_event: DomainEvent,
        order_result: OrderResult | None,
        snapshot: AccountSnapshot | None,
        execution: TradingReviewResult | None = None,
        plan: EntryPlan | None = None,
    ) -> "TradingDecisionWorkerResult":
        return await self._order_result_processor.handle(
            source_event=source_event,
            order_result=order_result,
            snapshot=snapshot,
            execution=execution,
            plan=plan,
        )

    async def _handle_position_updated(
        self,
        event: DomainEvent,
        snapshot: AccountSnapshot | None,
    ) -> "TradingDecisionWorkerResult":
        if snapshot is None:
            return TradingDecisionWorkerResult(
                entry_event=event,
                plan=None,
                review=None,
                emitted_event=event,
                state_after=None,
            )
        market = self._market_fromsnapshot_position(snapshot, event.condition_id, event.token_id)
        position = _match_position_for_event(snapshot, event)
        if market is not None:
            self._transition_market(market, MarketLifecycle.POSITION_OPEN)
        if position is None:
            return TradingDecisionWorkerResult(
                entry_event=event,
                plan=None,
                review=None,
                emitted_event=event,
                state_after=self._state_for_market(market),
            )

        exit_result = await self._execute_position_exit_if_needed(
            event=event,
            snapshot=snapshot,
            position=position,
        )
        if exit_result is not None:
            return exit_result
        return TradingDecisionWorkerResult(
            entry_event=event,
            plan=None,
            review=None,
            emitted_event=event,
            state_after=self._state_for_market(market),
        )

    async def _execute_position_exit_if_needed(
        self,
        *,
        event: DomainEvent,
        snapshot: AccountSnapshot,
        position: Position,
    ) -> "TradingDecisionWorkerResult | None":
        """在真实持仓更新后让策略决定是否需要退出保护单。

        用户 WS 的成交可能晚于初次下单响应到达。此时热态里已经有持仓，但
        同步 BUY 结果不一定触发跟单 SELL；这里以 position/open_orders 为事实，
        调策略 ``decide_exit``。当前策略默认等待结算会返回 SKIP；如策略返回
        SELL intent，仍走统一风控和执行器。
        """

        market = self._trading_decision_service.resolve_market(
            condition_id=position.condition_id,
            token_id=position.token_id,
        )
        open_orders = snapshot.open_orders_for_market(position.condition_id, position.token_id)
        decision = self._trading_decision_service.decide_exit(
            ExtensionContext(
                trace_id=event.trace_id,
                strategy_id=self._trading_decision_service.strategy_id,
                market=market,
                token_id=position.token_id,
                orderbook=self._trading_decision_service.lookup_orderbook(position.token_id),
                market_token_views=tuple(
                    MarketTokenView(
                        token_id=outcome.token_id,
                        outcome=outcome.outcome,
                    )
                    for outcome in (() if market is None else market.outcomes)
                ),
                account_snapshot=snapshot,
                position=position,
                open_orders=open_orders,
                metadata={
                    "exit_trigger": "position_updated",
                    "source_event_id": event.event_id,
                    "source_reason": event.reason,
                },
            )
        )
        if decision.action != ExtensionAction.SELL:
            return None
        intent = self._trading_decision_service.build_intent_from_decision(
            trace_id=event.trace_id,
            condition_id=position.condition_id,
            market_slug=event.market_slug or position.market_slug or (
                None if market is None else market.market_slug
            ),
            default_token_id=position.token_id,
            decision=decision,
        )
        if intent is None:
            return None

        review = await self._execute_managed_intent(intent, snapshot=snapshot)
        emitted = await self._publish(
            DomainEventType.ORDER_SUBMITTED,
            trace_id=intent.trace_id,
            market_slug=intent.market_slug,
            condition_id=intent.condition_id,
            token_id=intent.token_id,
            reason="" if review.order_result is None else review.order_result.reason,
            payload={
                "phase": "position_exit",
                "source_event_id": event.event_id,
                "decision_metadata": dict(decision.metadata),
                "intent": serialize_intent(intent),
                "review": serialize_review(review),
            },
        )
        self._apply_position_exit_result(
            review,
            intent=intent,
            snapshot=snapshot,
        )
        return TradingDecisionWorkerResult(
            entry_event=event,
            plan=None,
            review=review,
            emitted_event=emitted,
            emitted_events=(emitted,),
            follow_up_intents=(intent,),
            follow_up_reviews=(review,),
            state_after=self._state_for_market_by_key(position.condition_id),
        )

    def _apply_position_exit_result(
        self,
        review: TradingReviewResult,
        *,
        intent: ManagedOrderIntent,
        snapshot: AccountSnapshot,
    ) -> AccountSnapshot:
        """把持仓触发的退出单结果投影回热态。"""

        if review.order_result is None:
            return snapshot
        self._transition_from_order_result(review.order_result)
        projector = self._account_projector()
        if projector is not None and isinstance(intent, SellOrderIntent):
            projector.apply_sell_result(
                review.order_result,
                snapshot=snapshot,
                intent=intent,
            )
        if projector is not None:
            projector.apply_result_flags(review.order_result, snapshot=snapshot)
        if self._account_state_store is not None:
            return self._account_state_store.snapshot()
        return snapshot

    async def _emit_entry_skip(
        self,
        *,
        event: DomainEvent,
        plan: EntryPlan,
        trace_id: str,
        reason: str,
        lifecycle: MarketLifecycle,
        extra_payload: Mapping[str, object] | None,
        emit_in_tuple: bool,
    ) -> "TradingDecisionWorkerResult":
        """统一发布入场 SKIP 事件 + 状态转换 + 构造 result。

        ``emit_in_tuple`` 控制是否把 SKIP 事件同时写入 ``emitted_events`` 元组。
        历史上两条入场跳过路径（entry_paused / entry_not_ready）只在该字段、
        ``trace_id`` 来源、``reason`` 和 ``extra_payload`` 上有差别，其余完全相同。
        """

        payload: dict[str, object] = {
            "entry_event_id": event.event_id,
            "origin": TRADING_DECISION_WORKER_ORIGIN,
            "reason": reason,
            "allocation_plan": serialize_allocation_plan(plan),
            "allocation": serialize_allocation(plan),
            "plan_metadata": serialize_plan_metadata(plan),
        }
        if extra_payload:
            payload.update(extra_payload)
        skipped = await self._publish(
            DomainEventType.SKIPPED,
            trace_id=trace_id,
            market_slug=event.market_slug,
            condition_id=event.condition_id,
            token_id=event.token_id,
            reason=reason,
            payload=payload,
            priority=OutboxPriority.P3,
        )
        self._transition_market(plan.market, lifecycle if plan.market else None)
        emitted_events_tuple: tuple[DomainEvent, ...] = (skipped,) if emit_in_tuple else ()
        return TradingDecisionWorkerResult(
            entry_event=event,
            plan=plan,
            review=None,
            emitted_event=skipped,
            emitted_events=emitted_events_tuple,
            state_after=self._state_for_market(plan.market),
        )

    async def _publish(
        self,
        event_type: DomainEventType,
        *,
        trace_id: str,
        market_slug: str | None,
        condition_id: str | None,
        token_id: str | None,
        reason: str = "",
        payload: Mapping[str, object] | None = None,
        priority: OutboxPriority = OutboxPriority.P0,
    ) -> DomainEvent:
        payload_dict: dict[str, object] = {"origin": TRADING_DECISION_WORKER_ORIGIN}
        if payload is not None:
            payload_dict.update(payload)
        event = DomainEvent(
            trace_id=trace_id,
            event_type=event_type,
            event_id=uuid4().hex,
            market_slug=market_slug,
            condition_id=condition_id,
            token_id=token_id,
            reason=reason,
            created_at=_utc_now(),
            payload=payload_dict,
        )
        if self._event_bus is not None:
            await self._event_bus.publish(priority, event)
        return event

    def _entry_metadata(
        self,
        event: DomainEvent,
        snapshot: AccountSnapshot | None,
    ) -> dict[str, object]:
        """构造入场策略 metadata。

        Worker 只合并事件事实和外部 provider 提供的补充事实，不解释具体策略字段。
        """

        metadata: dict[str, object] = dict(event.payload)
        if self._entry_metadata_provider is None:
            return metadata
        try:
            extra_metadata = self._entry_metadata_provider(event, snapshot)
        except Exception as exc:
            metadata["entry_metadata_provider_error"] = str(exc)
            return metadata
        if extra_metadata:
            metadata.update(dict(extra_metadata))
        return metadata

    def _snapshot(self) -> AccountSnapshot | None:
        if self._account_state_store is not None:
            return self._account_state_store.snapshot()
        if self._balance_usdc is None and self._allowance_usdc is None:
            return None
        return AccountSnapshot(
            balance_usdc=self._balance_usdc or Decimal("0"),
            allowance_usdc=self._allowance_usdc or Decimal("0"),
            positions=tuple(self._positions_provider()),
            open_orders=tuple(self._open_orders_provider()),
            allow_new_entries=True,
        )

    def _build_positions_provider(self) -> PositionsProvider:
        if self._account_state_store is None:
            return lambda: ()
        account_state_store = self._account_state_store
        return lambda: account_state_store.snapshot().positions

    def _build_open_orders_provider(self) -> OpenOrdersProvider:
        if self._account_state_store is None:
            return lambda: ()
        account_state_store = self._account_state_store
        return lambda: account_state_store.snapshot().open_orders

    def _transition_market(self, market: Market | None, lifecycle: MarketLifecycle | None) -> None:
        if market is None or lifecycle is None:
            return
        self._market_lifecycle[market.condition_id] = lifecycle

    def _transition_market_by_result(self, order_result: OrderResult, lifecycle: MarketLifecycle) -> None:
        market = market_from_result(order_result)
        self._transition_market(market, lifecycle)

    def _transition_from_order_result(self, order_result: OrderResult) -> None:
        if order_result.side is None:
            return
        side = str(order_result.side).upper()
        if side == "BUY":
            if order_result.status in {OrderResultStatus.FULL_FILL, OrderResultStatus.PARTIAL_FILL}:
                self._transition_market_by_result(order_result, MarketLifecycle.POSITION_OPEN)
            elif order_result.status == OrderResultStatus.LIVE:
                self._transition_market_by_result(order_result, MarketLifecycle.PAUSED)
            elif order_result.status == OrderResultStatus.NO_FILL:
                self._transition_market_by_result(order_result, MarketLifecycle.ENTRY_READY)
            elif order_result.status in {OrderResultStatus.REJECTED, OrderResultStatus.FAILED, OrderResultStatus.UNKNOWN_TIMEOUT}:
                self._transition_market_by_result(order_result, MarketLifecycle.ENTRY_REJECTED)
        elif side == "SELL":
            if order_result.status in {OrderResultStatus.LIVE, OrderResultStatus.PARTIAL_FILL}:
                self._transition_market_by_result(order_result, MarketLifecycle.FOLLOW_UP_ORDER_OPEN)
            elif order_result.status == OrderResultStatus.FULL_FILL:
                self._transition_market_by_result(order_result, MarketLifecycle.POSITION_OPEN)

    def _pause_market(self, condition_id: str | None, *, reason: str) -> None:
        if condition_id is None:
            return
        self._market_lifecycle[condition_id] = MarketLifecycle.PAUSED
        if self._account_state_store is not None:
            self._account_state_store.pause_market(
                condition_id,
                reason=reason,
                source=MarketPauseSource.RISK,
            )

    def _state_for_market(self, market: Market | None) -> MarketLifecycle | None:
        if market is None:
            return None
        return self._market_lifecycle.get(market.condition_id)

    def _state_for_market_by_key(self, condition_id: str | None) -> MarketLifecycle | None:
        if condition_id is None:
            return None
        return self._market_lifecycle.get(condition_id)

    def _market_fromsnapshot_position(
        self,
        snapshot: AccountSnapshot,
        condition_id: str | None,
        token_id: str | None,
    ) -> Market | None:
        if condition_id is None or token_id is None:
            return None
        position = snapshot.get_position(condition_id, token_id)
        if position is None:
            return None
        market = self._trading_decision_service.resolve_market(condition_id=condition_id, token_id=token_id)
        return market

    async def _execute_managed_intent(
        self,
        intent: ManagedOrderIntent,
        *,
        snapshot: AccountSnapshot | None,
    ) -> TradingReviewResult:
        market = self._trading_decision_service.resolve_market(
            condition_id=intent.condition_id,
            token_id=intent.token_id,
        )
        if isinstance(intent, CancelOrderIntent):
            return await self._trading_service.cancel(intent)
        if isinstance(intent, ReplaceOrderIntent):
            return await self._trading_service.replace(intent)
        return await self._trading_service.review_intent(
            intent,
            market=market,
            orderbook=self._trading_decision_service.lookup_orderbook(intent.token_id),
            position=snapshot_position(snapshot, intent.condition_id, intent.token_id),
            open_orders=(
                snapshot.open_orders_for_market(intent.condition_id, intent.token_id)
                if snapshot is not None
                else ()
            ),
            classification_passed=True,
            balance_usdc=self._balance_usdc if self._balance_usdc is not None else snapshot_available_usdc(snapshot),
            allowance_usdc=(
                self._allowance_usdc if self._allowance_usdc is not None else snapshot_allowance(snapshot)
            ),
            max_order_usdc=self._max_order_usdc,
            max_market_usdc=self._max_market_usdc,
            max_total_usdc=self._max_total_usdc,
            max_open_orders=self._max_open_orders,
            order_retry_limit=self._order_retry_limit,
            operation=intent.side.value.lower(),
        )

    def _account_projector(self) -> AccountStateProjector | None:
        if self._account_state_store is None:
            return None
        return AccountStateProjector(
            self._account_state_store,
            strategy_id=self._trading_decision_service.strategy_id,
        )


def _match_position(
    positions: tuple[Position, ...],
    condition_id: str,
    token_id: str,
) -> Position | None:
    for position in positions:
        if position.condition_id == condition_id and position.token_id == token_id:
            return position
    return None


def _plan_allows_position_increase(plan: EntryPlan) -> bool:
    """判断计划是否是策略显式标记的受控加仓。

    依据策略在决策对象上声明的 intent_tags（含 ``"scale_in"``）+ intent
    自身的 ``allow_open_exit_overlap`` 双重标记，避免读策略私有 metadata 字符串。
    """

    intent = plan.intent
    if intent is None or not getattr(intent, "allow_open_exit_overlap", False):
        return False
    tags = getattr(intent, "intent_tags", frozenset()) or frozenset()
    return "scale_in" in tags


def _state_allows_position_increase(state: MarketLifecycle, plan: EntryPlan) -> bool:
    """只有持仓相关生命周期允许策略受控加仓继续走主链路。"""

    return state in POSITION_INCREASE_LIFECYCLES and _plan_allows_position_increase(plan)


def _state_allows_entry_attempt(state: MarketLifecycle, plan: EntryPlan) -> bool:
    """判断当前生命周期是否允许继续处理新的入场信号。

    ENTRY_READY 表示上一轮没有形成外部持仓副作用，例如 FAK no-fill
    或风控层可重试拒绝；后续盘口变好时应继续评估。已有持仓或退出单时，
    仍只允许策略显式标记的受控加仓进入 BUY 主链路。
    """

    return state in ENTRY_ATTEMPT_LIFECYCLES or _state_allows_position_increase(state, plan)


def _match_open_orders(
    open_orders: tuple[Order, ...],
    condition_id: str,
    token_id: str,
) -> tuple[Order, ...]:
    return tuple(
        order
        for order in open_orders
        if order.condition_id == condition_id and order.token_id == token_id
    )


def _match_position_for_event(
    snapshot: AccountSnapshot,
    event: DomainEvent,
) -> Position | None:
    """从当前热态中找到 position update 对应的持仓。"""

    if event.condition_id is not None and event.token_id is not None:
        return snapshot.get_position(event.condition_id, event.token_id)
    payload_position = event.payload.get("position")
    if isinstance(payload_position, Mapping):
        condition_id = payload_position.get("condition_id")
        token_id = payload_position.get("token_id")
        if condition_id is not None and token_id is not None:
            return snapshot.get_position(str(condition_id), str(token_id))
    payload_positions = event.payload.get("positions")
    if isinstance(payload_positions, (list, tuple)):
        for payload_item in payload_positions:
            if not isinstance(payload_item, Mapping):
                continue
            condition_id = payload_item.get("condition_id")
            token_id = payload_item.get("token_id")
            if condition_id is None or token_id is None:
                continue
            position = snapshot.get_position(str(condition_id), str(token_id))
            if position is not None:
                return position
    return None
