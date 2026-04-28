from __future__ import annotations

from typing import Mapping, Protocol

from polymarket_trader.app.order_projection import (
    AccountStateProjector,
    has_unexpected_resting_order,
    released_budget,
)
from polymarket_trader.app.trading_decision_service import EntryPlan, TradingDecisionService
from polymarket_trader.app.trading_service import TradingReviewResult, TradingService
from polymarket_trader.domain.account import AccountSnapshot
from polymarket_trader.domain.events import DomainEvent, DomainEventType
from polymarket_trader.domain.market import Market
from polymarket_trader.domain.order import (
    CancelOrderIntent,
    ManagedOrderIntent,
    OrderResult,
    OrderResultStatus,
    OrderSide,
    SellOrderIntent,
)
from polymarket_trader.domain.state_machine import MarketLifecycle
from polymarket_trader.extension_api import ExtensionContext, MarketTokenView
from polymarket_trader.runtime.account_state import AccountStateStore
from polymarket_trader.serialization import jsonable
from polymarket_trader.workers.trading_decision_event_payloads import (
    TRADING_DECISION_WORKER_ORIGIN,
    coerce_order_result_from_event,
    result_event_type,
    serialize_control_intent,
    serialize_intent,
    serialize_order_result,
    serialize_review,
    snapshot_position,
)
from polymarket_trader.workers.trading_decision_worker_result import TradingDecisionWorkerResult


class TradingOrderResultHost(Protocol):
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
    ) -> DomainEvent: ...

    def _transition_from_order_result(self, order_result: OrderResult) -> None: ...

    def _transition_market_by_result(
        self,
        order_result: OrderResult,
        lifecycle: MarketLifecycle,
    ) -> None: ...

    def _pause_market(self, condition_id: str | None, *, reason: str) -> None: ...

    def _state_for_market(self, market: Market | None) -> MarketLifecycle | None: ...

    def _state_for_market_by_key(self, condition_id: str | None) -> MarketLifecycle | None: ...

    async def _execute_managed_intent(
        self,
        intent: ManagedOrderIntent,
        *,
        snapshot: AccountSnapshot | None,
    ) -> TradingReviewResult: ...

    def _account_projector(self) -> AccountStateProjector | None: ...


class TradingOrderResultProcessor:
    def __init__(
        self,
        *,
        host: TradingOrderResultHost,
        trading_decision_service: TradingDecisionService,
        trading_service: TradingService,
        account_state_store: AccountStateStore | None,
    ) -> None:
        self._host = host
        self._trading_decision_service = trading_decision_service
        self._trading_service = trading_service
        self._account_state_store = account_state_store

    async def handle(
        self,
        *,
        source_event: DomainEvent,
        order_result: OrderResult | None,
        snapshot: AccountSnapshot | None,
        execution: TradingReviewResult | None = None,
        plan: EntryPlan | None = None,
    ) -> TradingDecisionWorkerResult:
        if order_result is None:
            order_result = coerce_order_result_from_event(source_event)
        if order_result is None:
            return TradingDecisionWorkerResult(
                entry_event=source_event,
                plan=plan,
                review=execution,
                emitted_event=source_event,
                state_after=self._host._state_for_market(
                    plan.market if plan is not None else None
                ),
            )

        state_before = self._host._state_for_market_by_key(order_result.condition_id)
        event_type = result_event_type(order_result)
        result_event = await self._host._publish(
            event_type,
            trace_id=order_result.trace_id,
            market_slug=order_result.market_slug,
            condition_id=order_result.condition_id,
            token_id=order_result.token_id,
            reason=order_result.reason,
            payload={
                "origin": TRADING_DECISION_WORKER_ORIGIN,
                "source_event_id": source_event.event_id,
                "operation": execution.operation if execution is not None else "result",
                "order_result": serialize_order_result(order_result),
                "execution": None if execution is None else serialize_review(execution),
            },
        )
        self._host._transition_from_order_result(order_result)
        follow_up_intents: list[ManagedOrderIntent] = []
        follow_up_results: list[TradingReviewResult] = []
        active_snapshot = snapshot
        position_already_projected = bool(
            source_event.payload.get("position_projected")
            or source_event.payload.get("account_projected")
        )

        if order_result.side == OrderSide.BUY and order_result.status in {
            OrderResultStatus.FULL_FILL,
            OrderResultStatus.PARTIAL_FILL,
            OrderResultStatus.NO_FILL,
        }:
            projector = self._host._account_projector()
            if projector is not None and not position_already_projected:
                projector.apply_buy_result(order_result, snapshot=snapshot)
            active_snapshot = (
                self._account_state_store.snapshot()
                if self._account_state_store is not None
                else snapshot
            )
        elif (
            execution is not None
            and isinstance(execution.intent, SellOrderIntent)
            and order_result.side == OrderSide.SELL
        ):
            projector = self._host._account_projector()
            if projector is not None and not position_already_projected:
                projector.apply_sell_result(
                    order_result,
                    snapshot=snapshot,
                    intent=execution.intent,
                )
            active_snapshot = (
                self._account_state_store.snapshot()
                if self._account_state_store is not None
                else snapshot
            )

        if order_result.side is not None and order_result.side.value == "BUY":
            if order_result.status in {
                OrderResultStatus.FULL_FILL,
                OrderResultStatus.PARTIAL_FILL,
            }:
                await self._publish_budget_released(
                    order_result,
                    state="entry_result_updated",
                )
            elif order_result.status == OrderResultStatus.NO_FILL:
                await self._publish_budget_released(
                    order_result,
                    state="no_fill_released",
                )
                self._host._transition_market_by_result(
                    order_result,
                    MarketLifecycle.ENTRY_READY,
                )
            elif has_unexpected_resting_order(order_result):
                cancel_review = await self._cancel_unexpected_resting_order(
                    order_result,
                    follow_up_intents,
                    follow_up_results,
                )
                if (
                    cancel_review.order_result is not None
                    and cancel_review.order_result.status == OrderResultStatus.CANCELLED
                ):
                    self._host._transition_from_order_result(cancel_review.order_result)
                self._host._pause_market(
                    order_result.condition_id,
                    reason="unexpected_resting_order",
                )
            elif order_result.status in {
                OrderResultStatus.REJECTED,
                OrderResultStatus.FAILED,
                OrderResultStatus.UNKNOWN_TIMEOUT,
            }:
                await self._host._publish(
                    DomainEventType.ORDER_STATE_UPDATED,
                    trace_id=order_result.trace_id,
                    market_slug=order_result.market_slug,
                    condition_id=order_result.condition_id,
                    token_id=order_result.token_id,
                    reason=order_result.reason,
                    payload={
                        "origin": TRADING_DECISION_WORKER_ORIGIN,
                        "state": "rejected",
                        "order_result": serialize_order_result(order_result),
                    },
                )
                self._host._transition_market_by_result(
                    order_result,
                    MarketLifecycle.ENTRY_REJECTED,
                )

        active_snapshot, result_event = await self._execute_follow_up_decisions(
            order_result=order_result,
            active_snapshot=active_snapshot,
            result_event=result_event,
            follow_up_intents=follow_up_intents,
            follow_up_results=follow_up_results,
        )

        projector = self._host._account_projector()
        if projector is not None:
            projector.apply_result_flags(order_result, snapshot=snapshot)
        return TradingDecisionWorkerResult(
            entry_event=source_event,
            plan=plan,
            review=execution,
            emitted_event=result_event,
            emitted_events=(result_event,),
            follow_up_intents=tuple(follow_up_intents),
            follow_up_reviews=tuple(follow_up_results),
            state_before=state_before,
            state_after=self._host._state_for_market_by_key(order_result.condition_id),
        )

    async def _publish_budget_released(
        self,
        order_result: OrderResult,
        *,
        state: str,
    ) -> None:
        released_budget_usdc = released_budget(order_result)
        await self._host._publish(
            DomainEventType.ORDER_STATE_UPDATED,
            trace_id=order_result.trace_id,
            market_slug=order_result.market_slug,
            condition_id=order_result.condition_id,
            token_id=order_result.token_id,
            reason="budget_released",
            payload={
                "origin": TRADING_DECISION_WORKER_ORIGIN,
                "state": state,
                "released_budget_usdc": str(released_budget_usdc),
                "order_result": serialize_order_result(order_result),
            },
        )

    async def _cancel_unexpected_resting_order(
        self,
        order_result: OrderResult,
        follow_up_intents: list[ManagedOrderIntent],
        follow_up_results: list[TradingReviewResult],
    ) -> TradingReviewResult:
        await self._host._publish(
            DomainEventType.ORDER_STATE_UPDATED,
            trace_id=order_result.trace_id,
            market_slug=order_result.market_slug,
            condition_id=order_result.condition_id,
            token_id=order_result.token_id,
            reason=order_result.reason or "unexpected_resting_order",
            payload={
                "origin": TRADING_DECISION_WORKER_ORIGIN,
                "state": "unexpected_resting_order",
                "order_result": serialize_order_result(order_result),
            },
        )
        cancel_intent = CancelOrderIntent(
            trace_id=order_result.trace_id,
            condition_id=order_result.condition_id,
            token_id=order_result.token_id,
            order_id=order_result.order_id
            or f"{order_result.trace_id}:{order_result.condition_id}:{order_result.token_id}",
            market_slug=order_result.market_slug,
            idempotency_key=(
                f"{order_result.trace_id}:{order_result.condition_id}:"
                f"{order_result.token_id}:cancel"
            ),
            reason="unexpected_resting_order",
        )
        follow_up_intents.append(cancel_intent)
        cancel_review = await self._trading_service.cancel(cancel_intent)
        follow_up_results.append(cancel_review)
        await self._host._publish(
            DomainEventType.ORDER_CANCEL_REQUESTED,
            trace_id=cancel_intent.trace_id,
            market_slug=cancel_intent.market_slug,
            condition_id=cancel_intent.condition_id,
            token_id=cancel_intent.token_id,
            reason=cancel_intent.reason,
            payload={
                "origin": TRADING_DECISION_WORKER_ORIGIN,
                "cancel_intent": serialize_control_intent(cancel_intent),
                "order_result": serialize_order_result(order_result),
            },
        )
        if (
            cancel_review.order_result is not None
            and cancel_review.order_result.status == OrderResultStatus.CANCELLED
        ):
            await self._host._publish(
                DomainEventType.ORDER_CANCELLED,
                trace_id=cancel_intent.trace_id,
                market_slug=cancel_intent.market_slug,
                condition_id=cancel_intent.condition_id,
                token_id=cancel_intent.token_id,
                reason=cancel_review.order_result.reason,
                payload={
                    "origin": TRADING_DECISION_WORKER_ORIGIN,
                    "cancel_review": serialize_review(cancel_review),
                    "order_result": serialize_order_result(cancel_review.order_result),
                },
            )
        return cancel_review

    async def _execute_follow_up_decisions(
        self,
        *,
        order_result: OrderResult,
        active_snapshot: AccountSnapshot | None,
        result_event: DomainEvent,
        follow_up_intents: list[ManagedOrderIntent],
        follow_up_results: list[TradingReviewResult],
    ) -> tuple[AccountSnapshot | None, DomainEvent]:
        resolved_market = self._trading_decision_service.resolve_market(
            condition_id=order_result.condition_id,
            token_id=order_result.token_id,
        )
        follow_up_decisions = self._trading_decision_service.decide_follow_up(
            ExtensionContext(
                trace_id=order_result.trace_id,
                market=resolved_market,
                token_id=order_result.token_id,
                market_token_views=tuple(
                    MarketTokenView(
                        token_id=outcome.token_id,
                        outcome=outcome.outcome,
                    )
                    for outcome in (() if resolved_market is None else resolved_market.outcomes)
                ),
                account_snapshot=active_snapshot,
                position=snapshot_position(
                    active_snapshot,
                    order_result.condition_id,
                    order_result.token_id,
                ),
                open_orders=(
                    active_snapshot.open_orders_for_market(
                        order_result.condition_id,
                        order_result.token_id,
                    )
                    if active_snapshot is not None
                    else ()
                ),
                order_result=order_result,
            )
        )
        for decision in follow_up_decisions:
            intent = self._trading_decision_service.build_intent_from_decision(
                trace_id=order_result.trace_id,
                condition_id=order_result.condition_id,
                market_slug=order_result.market_slug,
                default_token_id=order_result.token_id,
                decision=decision,
            )
            if intent is None:
                continue
            follow_up_intents.append(intent)
            follow_up_review = await self._host._execute_managed_intent(
                intent,
                snapshot=active_snapshot,
            )
            follow_up_results.append(follow_up_review)
            result_event = await self._host._publish(
                DomainEventType.ORDER_SUBMITTED,
                trace_id=intent.trace_id,
                market_slug=intent.market_slug,
                condition_id=intent.condition_id,
                token_id=intent.token_id,
                reason=(
                    ""
                    if follow_up_review.order_result is None
                    else follow_up_review.order_result.reason
                ),
                payload={
                    "origin": TRADING_DECISION_WORKER_ORIGIN,
                    "phase": "follow_up",
                    "source_order_result": serialize_order_result(order_result),
                    "decision_metadata": jsonable(decision.metadata),
                    "intent": serialize_intent(intent),
                    "review": serialize_review(follow_up_review),
                },
            )
            active_snapshot = self._apply_follow_up_result(
                follow_up_review,
                intent=intent,
                active_snapshot=active_snapshot,
            )
        return active_snapshot, result_event

    def _apply_follow_up_result(
        self,
        follow_up_review: TradingReviewResult,
        *,
        intent: ManagedOrderIntent,
        active_snapshot: AccountSnapshot | None,
    ) -> AccountSnapshot | None:
        if follow_up_review.order_result is None:
            return active_snapshot
        self._host._transition_from_order_result(follow_up_review.order_result)
        if isinstance(intent, SellOrderIntent):
            projector = self._host._account_projector()
            if projector is not None:
                projector.apply_sell_result(
                    follow_up_review.order_result,
                    snapshot=active_snapshot,
                    intent=intent,
                )
        projector = self._host._account_projector()
        if projector is not None:
            projector.apply_result_flags(
                follow_up_review.order_result,
                snapshot=active_snapshot,
            )
        return (
            self._account_state_store.snapshot()
            if self._account_state_store is not None
            else active_snapshot
        )
